"""
Self-contained offline unit tests for tiktok-ssr-engine.
Does not require internet access, live servers, or proxies.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from config import get_indo_proxies, get_next_proxy, get_proxy_pool, get_warp_proxies
from extractor import (
    TikTokExtractError,
    TikTokGeoBlockedError,
    TikTokInfraError,
    TikTokRegionRestrictedError,
    TikTokSSRExtractor,
    _classify_error,
    _decode_ssstik_url,
    _first_url,
    build_filename,
    sanitize_filename_part,
    solve_slardar_challenge,
)
from proxy_health import apply_block_verdicts, classify_block_response
from proxy_state import (
    clear_proxy_blocked,
    dec_in_flight,
    get_in_flight,
    get_in_flight_many,
    inc_in_flight,
    is_proxy_blocked,
    is_proxy_usable,
    mark_proxy_blocked,
    mark_proxy_cooldown,
    mark_proxy_dead,
    mark_proxy_usable,
)
from session import (
    create_session,
    get_cached_extraction,
    get_session,
    set_cached_extraction,
)


class TestTikTokSSROffline(unittest.TestCase):

    def test_sanitize_filename_part(self):
        self.assertEqual(sanitize_filename_part("User @Special!"), "User_Special")
        self.assertEqual(sanitize_filename_part(""), "tiktok")
        self.assertEqual(sanitize_filename_part("   "), "tiktok")

    def test_first_url_helper(self):
        self.assertEqual(_first_url("https://example.com/1.mp4"), "https://example.com/1.mp4")
        self.assertEqual(_first_url(["https://example.com/a.mp4", "https://example.com/b.mp4"]), "https://example.com/a.mp4")
        self.assertEqual(_first_url({"url": "https://example.com/c.mp4"}), "https://example.com/c.mp4")
        self.assertEqual(_first_url({"urlList": ["https://example.com/d.mp4"]}), "https://example.com/d.mp4")
        self.assertEqual(_first_url(None), "")

    def test_decode_ssstik_url(self):
        self.assertEqual(_decode_ssstik_url("https://example.com/video.mp4"), "https://example.com/video.mp4")
        raw = "aHR0cHM6Ly90aWt0b2tjZG4uY29tL3ZpZGVvLm1wNA=="
        self.assertEqual(_decode_ssstik_url(f"https://tikcdn.io/ssstik/{raw}"), "https://tiktokcdn.com/video.mp4")

    def test_session_cookie_dehydration(self):
        # Cookies are stored once and referenced, but callers must not notice:
        # get_session still hands back the full cookie string.
        cookie = "sid=" + ("a" * 800)
        keys = [create_session({
            "type": t, "direct_url": f"https://cdn.example.com/{t}", "cookies": cookie,
        }) for t in ("video", "mp3", "photo")]
        for k in keys:
            s = get_session(k)
            self.assertIsNotNone(s)
            self.assertEqual(s["cookies"], cookie)

    def test_session_without_cookies_still_works(self):
        k = create_session({"type": "video", "direct_url": "https://cdn.example.com/x.mp4"})
        s = get_session(k)
        self.assertEqual(s["direct_url"], "https://cdn.example.com/x.mp4")
        self.assertFalse(s.get("cookies"))

    def test_session_creation_and_retrieval(self):
        payload = {"type": "video", "direct_url": "https://cdn.example.com/test.mp4", "author": "tester"}
        key = create_session(payload)
        self.assertTrue(bool(key))
        
        retrieved = get_session(key)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["direct_url"], "https://cdn.example.com/test.mp4")
        self.assertEqual(retrieved["type"], "video")

    def test_extraction_caching(self):
        test_url = "https://www.tiktok.com/@test/video/998877665544"
        test_data = {"title": "Cached Title", "extract_source": "unit_test"}
        
        set_cached_extraction(test_url, test_data, ttl=60)
        cached = get_cached_extraction(test_url)
        
        self.assertIsNotNone(cached)
        self.assertEqual(cached["title"], "Cached Title")
        self.assertEqual(cached["extract_source"], "unit_test")

    def test_slardar_challenge_solver(self):
        self.assertIsNone(solve_slardar_challenge("<html><body>Normal Page</body></html>"))

    def test_build_response_from_ssr_video(self):
        extractor = TikTokSSRExtractor()
        dummy_item = {
            "desc": "Test Video Title #viral",
            "author": {
                "nickname": "Cool Creator",
                "uniqueId": "coolcreator",
            },
            "stats": {
                "playCount": 1000,
                "diggCount": 500,
            },
            "video": {
                "playAddr": "https://v16m.tiktokcdn.com/test_video.mp4",
                "duration": 15,
            },
            "music": {
                "playUrl": "https://v16m.tiktokcdn.com/test_audio.mp3",
                "duration": 15,
            }
        }
        
        res = extractor.build_response_from_ssr(dummy_item, "https://www.tiktok.com/@coolcreator/video/123", source="web_ssr")
        self.assertEqual(res["status"], "tunnel")
        self.assertEqual(res["title"], "Test Video Title #viral")
        self.assertEqual(res["artist"], "Cool Creator")
        self.assertIn("no_watermark", res["download_link"])
        self.assertIn("mp3", res["download_link"])

    def test_build_response_from_ssr_photo_slideshow(self):
        extractor = TikTokSSRExtractor()
        dummy_item = {
            "desc": "Photo Slideshow Post",
            "author": {"nickname": "Photo Artist", "uniqueId": "photoartist"},
            "imagePost": {
                "images": [
                    {"imageURL": "https://p16.tiktokcdn.com/img1.jpg"},
                    {"imageURL": "https://p16.tiktokcdn.com/img2.jpg"},
                ]
            },
            "music": {"playUrl": "https://v16m.tiktokcdn.com/audio.mp3", "duration": 10}
        }
        
        res = extractor.build_response_from_ssr(dummy_item, "https://www.tiktok.com/@photoartist/photo/456", source="web_ssr")
        self.assertEqual(res["status"], "picker")
        self.assertEqual(len(res["photos"]), 2)
        self.assertIn("download_slideshow", res)
        
        ss_key = res["download_slideshow"].split("key=")[1]
        session = get_session(ss_key)
        self.assertEqual(session["type"], "slideshow")

    def test_build_response_from_frontity_slideshow_render_fix(self):
        extractor = TikTokSSRExtractor()
        dummy_vdata = {
            "itemInfos": {"text": "Embed Slideshow", "covers": ["https://p16.tiktokcdn.com/cover.jpg"]},
            "authorInfos": {"nickName": "Embed User", "uniqueId": "embeduser"},
            "musicInfos": {"playUrl": ["https://v16m.tiktokcdn.com/music.mp3"]},
            "imagePostInfo": {
                "images": [
                    {"displayImage": {"urlList": ["https://p16.tiktokcdn.com/slide1.jpg"]}},
                    {"displayImage": {"urlList": ["https://p16.tiktokcdn.com/slide2.jpg"]}},
                ]
            }
        }
        
        res = extractor.build_response_from_frontity(dummy_vdata, "https://www.tiktok.com/@embeduser/photo/789", source="web_embed_ssr")
        self.assertEqual(res["status"], "picker")
        self.assertIn("download_slideshow", res)
        
        ss_key = res["download_slideshow"].split("key=")[1]
        session = get_session(ss_key)
        self.assertEqual(session["type"], "slideshow")

    def test_proxy_pool_zigzag_distribution(self):
        with patch("config.PROXY_COUNT", 50):
            pool = get_proxy_pool()
            self.assertEqual(len(pool), 50)
            
            geo_indices = []
            for i, p in enumerate(pool):
                num = int(p.split("-")[1].split(":")[0])
                if 1 <= num <= 17:
                    geo_indices.append(i)
            
    def test_codec_prioritization_h264(self):
        extractor = TikTokSSRExtractor()
        dummy_item = {
            "desc": "Video with Multiple Codecs",
            "author": {"nickname": "Codec Tester", "uniqueId": "codecs"},
            "video": {
                "playAddr": "https://v16m.tiktokcdn.com/default.mp4",
                "bitrateInfo": [
                    # Higher bitrate but unplayable bytevc2 codec -> should be skipped!
                    {"Bitrate": 2500000, "CodecType": "bytevc2", "GearName": "bytevc2_1080", "PlayAddr": {"UrlList": ["https://v16m.tiktokcdn.com/bytevc2_1080.mp4"]}},
                    # Higher bitrate bytevc1 (H.265)
                    {"Bitrate": 2000000, "CodecType": "bytevc1", "GearName": "bytevc1_720", "PlayAddr": {"UrlList": ["https://v16m.tiktokcdn.com/bytevc1_720.mp4"]}},
                    # H.264 (AVC1) -> should be selected as best compatible HD!
                    {"Bitrate": 1500000, "CodecType": "h264", "GearName": "h264_720", "PlayAddr": {"UrlList": ["https://v16m.tiktokcdn.com/h264_720.mp4"]}},
                ]
            }
        }
        res = extractor.build_response_from_ssr(dummy_item, "https://www.tiktok.com/@codecs/video/100")
        hd_link = res["download_link"]["no_watermark_hd"]
        session = get_session(hd_link.split("key=")[1])
        # Verify H.264 was prioritized over bytevc1/bytevc2
        self.assertEqual(session["direct_url"], "https://v16m.tiktokcdn.com/h264_720.mp4")

    def test_subtitles_extraction(self):
        extractor = TikTokSSRExtractor()
        dummy_item = {
            "desc": "Video with Subtitles",
            "author": {"nickname": "Sub Creator", "uniqueId": "subs"},
            "video": {
                "playAddr": "https://v16m.tiktokcdn.com/video.mp4",
                "subtitleInfos": [
                    {"LanguageCodeName": "en", "LanguageName": "English", "Url": "https://p16.tiktokcdn.com/sub_en.vtt", "Format": "webvtt"},
                    {"LanguageCodeName": "id", "LanguageName": "Indonesian", "Url": "https://p16.tiktokcdn.com/sub_id.vtt", "Format": "webvtt"},
                ]
            }
        }
        res = extractor.build_response_from_ssr(dummy_item, "https://www.tiktok.com/@subs/video/200")
        self.assertIn("subtitles", res)
        self.assertIn("en", res["subtitles"])
        self.assertIn("id", res["subtitles"])
        self.assertEqual(res["subtitles"]["en"][0]["url"], "https://p16.tiktokcdn.com/sub_en.vtt")


    def test_indo_proxy_pool(self):
        with patch("config.PROXY_COUNT", 50):
            pool = get_indo_proxies()
            self.assertEqual(pool, [
                "socks5h://wireproxy-01:1080",
                "socks5h://wireproxy-13:1080",
                "socks5h://wireproxy-14:1080",
            ])
        with patch("config.PROXY_COUNT", 5):
            self.assertEqual(get_indo_proxies(), ["socks5h://wireproxy-01:1080"])

    def test_warp_only_selection_is_strict(self):
        with patch("config.PROXY_COUNT", 21):
            warp = get_warp_proxies()
            self.assertEqual(len(warp), 4)
            for _ in range(10):
                self.assertIn(get_next_proxy(warp_only=True), warp)

            # With every WARP node blocked the strict lane yields nothing, so the
            # caller can decide the fallback instead of silently getting a geo node.
            for w in warp:
                mark_proxy_blocked(w, 60)
            try:
                self.assertIsNone(get_next_proxy(warp_only=True))
                self.assertNotIn(get_next_proxy(prefer_geo=True), warp)
            finally:
                for w in warp:
                    clear_proxy_blocked(w)

    def test_blocked_proxy_is_not_usable(self):
        p = "socks5h://wireproxy-18:1080"
        self.assertFalse(is_proxy_blocked(p))
        mark_proxy_blocked(p, 60)
        self.assertTrue(is_proxy_blocked(p))
        self.assertFalse(is_proxy_usable(p))
        clear_proxy_blocked(p)
        self.assertTrue(is_proxy_usable(p))

    def test_block_classification(self):
        # Marker present means TikTok served real data: never a block.
        self.assertFalse(classify_block_response(200, "", 313000, "x", marker_present=True))
        # The ~44KB JS shell is a degraded page, not a block: embed SSR still works.
        self.assertFalse(classify_block_response(200, "", 44061, "<html>shell</html>"))
        # Tiny stub, WAF codes, and region interstitials are blocks.
        self.assertTrue(classify_block_response(200, "", 1462, "<html/>"))
        self.assertTrue(classify_block_response(403, "", 900, ""))
        self.assertTrue(classify_block_response(429, "", 900, ""))
        self.assertTrue(classify_block_response(302, "https://www.tiktok.com/hk/about", 136, ""))
        # A WAF verdict outranks a healthy-looking body with the marker present.
        self.assertTrue(classify_block_response(
            200, "", 419000, '{"statusCode":10204}', marker_present=True))

    def test_block_sanity_ratio_fails_open(self):
        pool = {f"socks5h://wireproxy-{i:02d}:1080": True for i in range(1, 21)}
        # A probe condemning the whole pool is broken, not evidence: commit nothing.
        self.assertFalse(apply_block_verdicts(pool))
        for url in pool:
            self.assertFalse(is_proxy_blocked(url))

        # A believable minority of blocks is committed normally.
        mixed = {f"socks5h://wireproxy-{i:02d}:1080": (i <= 3) for i in range(1, 21)}
        try:
            self.assertTrue(apply_block_verdicts(mixed))
            self.assertTrue(is_proxy_blocked("socks5h://wireproxy-01:1080"))
            self.assertFalse(is_proxy_blocked("socks5h://wireproxy-19:1080"))
        finally:
            for url in mixed:
                clear_proxy_blocked(url)

    def test_error_classification(self):
        class DummyErr(Exception):
            pass

        e = DummyErr("boom")
        e.code = 5
        self.assertIsInstance(_classify_error(e), TikTokInfraError)

        e2 = DummyErr("timed out after 15s")
        self.assertIsInstance(_classify_error(e2), TikTokInfraError)

        e3 = DummyErr("could not resolve proxy")
        self.assertIsInstance(_classify_error(e3), TikTokInfraError)

        geo = TikTokGeoBlockedError()
        self.assertIsInstance(_classify_error(geo), TikTokGeoBlockedError)

        region = TikTokRegionRestrictedError()
        self.assertIsInstance(_classify_error(region), TikTokRegionRestrictedError)

        generic = _classify_error(DummyErr("some parse error"))
        self.assertIsInstance(generic, TikTokExtractError)
        self.assertNotIsInstance(generic, TikTokInfraError)

    def test_build_filename_photo(self):
        s = {"type": "photo", "photo_index": 3, "author": "M_Yusuf"}
        self.assertEqual(build_filename(s), "M_Yusuf_photo_3.jpeg")

    def test_build_filename_video(self):
        s = {"type": "video", "quality": "no_watermark_hd", "author": "Joey_Batt"}
        self.assertEqual(build_filename(s), "Joey_Batt_video_no_watermark_hd.mp4")

    def test_build_filename_mp3(self):
        s = {"type": "mp3", "author": "M_Yusuf"}
        self.assertEqual(build_filename(s), "M_Yusuf_mp3.mp3")

    def test_build_filename_slideshow(self):
        s = {"type": "slideshow", "author": "M_Yusuf"}
        self.assertEqual(build_filename(s), "M_Yusuf_slideshow.mp4")

    def test_build_filename_fallbacks(self):
        self.assertEqual(build_filename({"type": "video", "author": "X"}), "X_video.mp4")
        self.assertEqual(build_filename({"author": "X"}), "X_video.mp4")
        self.assertEqual(build_filename({"type": "photo", "author": "A", "index": 2}), "A_photo_2.jpeg")

    def test_proxy_state_cooldown(self):
        mark_proxy_cooldown("socks5h://wireproxy-01:1080", 5)
        self.assertFalse(is_proxy_usable("socks5h://wireproxy-01:1080"))
        self.assertTrue(is_proxy_usable("socks5h://wireproxy-99:1080"))
        mark_proxy_usable("socks5h://wireproxy-01:1080")
        self.assertTrue(is_proxy_usable("socks5h://wireproxy-01:1080"))

    def test_proxy_state_dead_expiry(self):
        # Negative TTL, not 0: with 0 the mark expires exactly at now, so the test
        # depended on the clock ticking between marking and checking and failed
        # roughly one run in five.
        mark_proxy_dead("socks5h://wireproxy-02:1080", -1)
        self.assertTrue(is_proxy_usable("socks5h://wireproxy-02:1080"))
        mark_proxy_dead("socks5h://wireproxy-02:1080", 5)
        self.assertFalse(is_proxy_usable("socks5h://wireproxy-02:1080"))
        mark_proxy_usable("socks5h://wireproxy-02:1080")

    def test_in_flight_counter(self):
        inc_in_flight("p1")
        inc_in_flight("p1")
        self.assertEqual(get_in_flight("p1"), 2)
        dec_in_flight("p1")
        self.assertEqual(get_in_flight("p1"), 1)
        dec_in_flight("p1")
        self.assertEqual(get_in_flight("p1"), 0)
        # An unmatched decrement must not drive the counter negative, which would
        # make the proxy look idler than every other and soak up all traffic.
        dec_in_flight("p1")
        self.assertEqual(get_in_flight("p1"), 0)

    def test_in_flight_many_batches(self):
        for _ in range(3):
            inc_in_flight("batch-a")
        inc_in_flight("batch-b")
        try:
            loads = get_in_flight_many(["batch-a", "batch-b", "batch-never-used"])
            self.assertEqual(loads["batch-a"], 3)
            self.assertEqual(loads["batch-b"], 1)
            self.assertEqual(loads["batch-never-used"], 0)
            self.assertEqual(get_in_flight_many([]), {})
        finally:
            for _ in range(3):
                dec_in_flight("batch-a")
            dec_in_flight("batch-b")

    def test_pick_from_prefers_least_loaded_across_workers(self):
        pool = ["socks5h://wireproxy-01:1080", "socks5h://wireproxy-02:1080"]
        # Simulate load booked by a different worker: with per-process counters
        # this proxy would still look idle here.
        for _ in range(4):
            inc_in_flight(pool[0])
        try:
            import config
            for _ in range(10):
                self.assertEqual(config._pick_from(pool), pool[1])
        finally:
            for _ in range(4):
                dec_in_flight(pool[0])

    def test_get_next_proxy_skips_dead(self):
        with patch("config.is_proxy_usable", side_effect=lambda p: "-01:" not in p):
            with patch("config.PROXY_COUNT", 5):
                for _ in range(20):
                    p = get_next_proxy()
                    self.assertIsNotNone(p)
                    self.assertNotIn("-01:", p)

    def test_get_next_proxy_least_loaded(self):
        with patch("config.PROXY_COUNT", 3):
            p_busy = "socks5h://wireproxy-01:1080"
            p_idle = "socks5h://wireproxy-02:1080"
            inc_in_flight(p_busy)
            with patch("config.random.random", return_value=0.5):
                for _ in range(10):
                    self.assertEqual(get_next_proxy(), p_idle)
            dec_in_flight(p_busy)


if __name__ == "__main__":
    unittest.main()
