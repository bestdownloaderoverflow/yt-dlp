"""
Self-contained offline unit tests for tiktok-ssr-engine.
Does not require internet access, live servers, or proxies.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from config import (
    get_geo_proxies, get_indo_proxies, get_next_proxy, get_proxy_pool, get_warp_proxies,
)
from extractor import (
    TikTokAccessRestrictedError,
    TikTokExtractError,
    TikTokIPBlockedError,
    TikTokInfraError,
    TikTokSSRExtractor,
    _classify_error,
    _decode_ssstik_url,
    _first_url,
    build_filename,
    sanitize_filename_part,
    solve_slardar_challenge,
)
from proxy_health import (
    apply_block_verdicts,
    classify_block_response,
    probe_proxy,
    record_probe_success,
)
from main import (
    clear_proxy_and_shared_exit_blocked,
    extraction_cache_key,
    extract_tiktok,
    mark_proxy_and_shared_exit_blocked,
    render_slideshow,
    should_cooldown_download_failure,
    stream_file_and_cleanup,
    should_retry_ip_block_via_indonesia,
    should_retry_via_indonesia,
    validate_tiktok_post_url,
    verify_ambiguous_ip_block,
)
from proxy_state import (
    clear_exit_block_strikes,
    clear_proxy_blocked,
    clear_proxy_exit_ip,
    clear_proxy_reconnect_state,
    dec_in_flight,
    get_proxy_exit_ip,
    get_proxy_control_verdict,
    get_in_flight,
    get_in_flight_many,
    inc_in_flight,
    is_proxy_blocked,
    is_proxy_usable,
    mark_proxy_blocked,
    mark_proxy_cooldown,
    mark_proxy_dead,
    mark_proxy_request_success,
    mark_proxy_tunnel_alive,
    mark_proxy_usable,
    process_proxy_reconnect,
    record_exit_block_strike,
    record_proxy_probe_failure,
    request_proxy_reconnect,
    set_proxy_exit_ip,
    set_proxy_control_verdict,
)
from session import (
    create_session,
    get_cached_extraction,
    get_geo_hint,
    get_session,
    set_cached_extraction,
    set_geo_hint,
)


class TestTikTokSSROffline(unittest.TestCase):

    def test_tiktok_url_validation_and_cache_normalization(self):
        url = validate_tiktok_post_url(
            "https://www.tiktok.com/@user/video/123?is_from_webapp=1&sender_device=pc"
        )
        self.assertEqual(extraction_cache_key(url), "https://www.tiktok.com/@user/video/123")
        with self.assertRaises(HTTPException):
            validate_tiktok_post_url("https://example.com/video/123")
        with self.assertRaises(HTTPException):
            validate_tiktok_post_url("https://www.tiktok.com/about")

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

    def test_country_preference_spreads_retries_out_of_a_failed_country(self):
        import config

        sg_a, sg_b = "socks5h://wireproxy-02:1080", "socks5h://wireproxy-07:1080"
        my = "socks5h://wireproxy-03:1080"
        with patch("config.PROXY_COUNTRIES", {sg_a: "SG", sg_b: "SG", my: "MY"}), \
                patch("proxy_state._redis_client", None):
            # Two of three candidates are Singapore, so picking by load alone
            # would usually stay there. Retrying inside a country that already
            # failed is what made "two distinct exits" weak evidence.
            for _ in range(10):
                self.assertEqual(
                    config._pick_from([sg_a, sg_b, my], avoid_countries={"SG"}), my)

    def test_country_preference_never_empties_the_pool(self):
        import config

        sg_a, sg_b = "socks5h://wireproxy-02:1080", "socks5h://wireproxy-07:1080"
        with patch("config.PROXY_COUNTRIES", {sg_a: "SG", sg_b: "SG"}), \
                patch("proxy_state._redis_client", None):
            # Preference, not filter: 8 of 17 geo nodes are Singapore, so a hard
            # country filter would strand requests that could still be served.
            self.assertIn(
                config._pick_from([sg_a, sg_b], avoid_countries={"SG"}), (sg_a, sg_b))

    def test_unknown_country_exits_stay_eligible(self):
        import config

        warp, sg = "socks5h://wireproxy-18:1080", "socks5h://wireproxy-02:1080"
        with patch("config.PROXY_COUNTRIES", {sg: "SG"}), \
                patch("proxy_state._redis_client", None):
            # WARP is anycast and has no fixed country. Absence of evidence must
            # not be read as a match against the avoided set.
            for _ in range(10):
                self.assertEqual(
                    config._pick_from([sg, warp], avoid_countries={"SG"}), warp)

    def test_indonesia_indexes_are_derived_from_the_pool_manifest(self):
        from config import derive_indonesia_indexes

        manifest = {
            1: {"index": 1, "country": "ID"},
            2: {"index": 2, "country": "SG"},
            13: {"index": 13, "country": "id"},
            18: {"index": 18, "country": None},
        }
        self.assertEqual(derive_indonesia_indexes(manifest), [1, 13])
        # An explicit setting still wins over the manifest.
        self.assertEqual(derive_indonesia_indexes(manifest, "4,5"), [4, 5])
        # No manifest and no override disables the lane rather than guessing.
        self.assertEqual(derive_indonesia_indexes({}), [])

    def test_proxy_pool_covers_every_subset(self):
        # The pool is the complete set: main.py and proxy_health.py rely on it
        # to fan block/reconnect state out to every node, so any proxy a subset
        # helper can hand out must appear in it.
        with patch("config.PROXY_COUNT", 50):
            pool = get_proxy_pool()
            self.assertEqual(len(pool), 50)
            self.assertEqual(len(set(pool)), 50)
            for subset in (get_warp_proxies(), get_geo_proxies(), get_indo_proxies()):
                self.assertTrue(subset)
                self.assertTrue(set(subset).issubset(set(pool)))

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

    def test_duplicate_video_variants_are_not_advertised(self):
        extractor = TikTokSSRExtractor()
        shared = {"UrlKey": "h264_1080p_same", "UrlList": ["https://v16.example/video/object.mp4"]}
        dummy_item = {
            "author": {"nickname": "Tester"},
            "video": {
                "playAddr": shared,
                "downloadAddr": shared,
                "hasWatermark": False,
                "bitrateInfo": [{
                    "Bitrate": 1500000,
                    "CodecType": "h264",
                    "GearName": "h264_1080",
                    "PlayAddr": shared,
                }],
            },
        }
        links = extractor.build_response_from_ssr(
            dummy_item, "https://www.tiktok.com/@tester/video/123"
        )["download_link"]
        self.assertEqual(set(links), {"no_watermark"})

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

    def test_indo_only_selection_is_strict(self):
        with patch("config.PROXY_COUNT", 21):
            indo = get_indo_proxies()
            for proxy in indo:
                mark_proxy_blocked(proxy, 60)
            try:
                self.assertIsNone(get_next_proxy(indo_only=True))
            finally:
                for proxy in indo:
                    clear_proxy_blocked(proxy)

    def test_blocked_proxy_is_not_usable(self):
        p = "socks5h://wireproxy-18:1080"
        self.assertFalse(is_proxy_blocked(p))
        mark_proxy_blocked(p, 60)
        self.assertTrue(is_proxy_blocked(p))
        self.assertFalse(is_proxy_usable(p))
        clear_proxy_blocked(p)
        self.assertTrue(is_proxy_usable(p))

    def test_tunnel_liveness_does_not_clear_tiktok_block(self):
        p = "socks5h://wireproxy-18:1080"
        mark_proxy_blocked(p, 60)
        try:
            mark_proxy_tunnel_alive(p)
            self.assertTrue(is_proxy_blocked(p))
            self.assertFalse(is_proxy_usable(p))
        finally:
            clear_proxy_blocked(p)

    def test_request_and_probe_block_strikes_do_not_add_up(self):
        # The two observers confirm blocks with their own thresholds. Sharing one
        # counter let one strike from each reach a confirmation neither asked for.
        exit_ip = "203.0.113.77"
        with patch("proxy_state._redis_client", None):
            try:
                self.assertEqual(
                    record_exit_block_strike(exit_ip, True, source="request"), 1)
                self.assertEqual(
                    record_exit_block_strike(exit_ip, True, source="probe"), 1)
                self.assertEqual(
                    record_exit_block_strike(exit_ip, True, source="request"), 2)
                self.assertEqual(
                    record_exit_block_strike(exit_ip, True, source="probe"), 2)
            finally:
                record_exit_block_strike(exit_ip, False)

    def test_serving_result_clears_every_observers_strikes(self):
        # Proof that TikTok serves an exit voids all suspicion of it, not just
        # the suspicion held by whoever observed the success.
        exit_ip = "203.0.113.78"
        with patch("proxy_state._redis_client", None):
            try:
                record_exit_block_strike(exit_ip, True, source="request")
                record_exit_block_strike(exit_ip, True, source="probe")
                self.assertEqual(clear_exit_block_strikes(exit_ip), 2)
                self.assertEqual(
                    record_exit_block_strike(exit_ip, True, source="request"), 1)
                self.assertEqual(
                    record_exit_block_strike(exit_ip, True, source="probe"), 1)
            finally:
                record_exit_block_strike(exit_ip, False)

    def test_official_tiktok_success_clears_old_exit_block_strike(self):
        p = "socks5h://wireproxy-18:1080"
        exit_ip = "192.0.2.88"
        try:
            set_proxy_exit_ip(p, exit_ip)
            self.assertEqual(record_exit_block_strike(exit_ip, True), 1)
            mark_proxy_request_success(p, tiktok_served=True)
            self.assertEqual(record_exit_block_strike(exit_ip, True), 1)
        finally:
            record_exit_block_strike(exit_ip, False)
            clear_proxy_exit_ip(p)

    def test_exit_ip_tracking(self):
        p = "socks5h://audit-proxy.invalid:1080"
        try:
            set_proxy_exit_ip(p, "203.0.113.9")
            self.assertEqual(get_proxy_exit_ip(p), "203.0.113.9")
        finally:
            clear_proxy_exit_ip(p)

    def test_control_verdict_cache_clears_when_exit_ip_changes(self):
        proxy = "socks5h://wireproxy-88:1080"
        try:
            set_proxy_exit_ip(proxy, "198.51.100.8")
            set_proxy_control_verdict(proxy, False, ttl_seconds=60)
            self.assertIs(get_proxy_control_verdict(proxy), False)
            set_proxy_exit_ip(proxy, "198.51.100.9")
            self.assertIsNone(get_proxy_control_verdict(proxy))
        finally:
            clear_proxy_exit_ip(proxy)

    def test_block_classification(self):
        # Marker present means TikTok served real data: never a block.
        self.assertIs(classify_block_response(200, "", 313000, "x", marker_present=True), False)
        # Shells, tiny stubs and 5xx responses are inconclusive, not IP blocks.
        self.assertIsNone(classify_block_response(200, "", 44061, "<html>shell</html>"))
        self.assertIsNone(classify_block_response(200, "", 1462, "<html/>"))
        self.assertIsNone(classify_block_response(503, "", 26, "Service Unavailable"))
        # Only hard WAF/rate-limit signals and region interstitials block.
        self.assertTrue(classify_block_response(403, "", 900, ""))
        self.assertTrue(classify_block_response(429, "", 900, ""))
        self.assertTrue(classify_block_response(302, "https://www.tiktok.com/hk/about", 136, ""))
        # A WAF verdict outranks a healthy-looking body with the marker present.
        self.assertTrue(classify_block_response(
            200, "", 419000, '{"statusCode":10204}', marker_present=True))

    def test_block_sanity_ratio_fails_open(self):
        pool = {f"socks5h://wireproxy-{i:02d}:1080": True for i in range(1, 21)}
        try:
            for i, url in enumerate(pool, start=1):
                set_proxy_exit_ip(url, f"203.0.113.{i}")
            # A probe condemning the whole unique-IP pool is broken: commit nothing.
            self.assertFalse(apply_block_verdicts(pool))
            for url in pool:
                self.assertFalse(is_proxy_blocked(url))

            # A believable minority needs two hard-block cycles before commit.
            mixed = {url: (i <= 3) for i, url in enumerate(pool, start=1)}
            self.assertTrue(apply_block_verdicts(mixed))
            self.assertFalse(is_proxy_blocked("socks5h://wireproxy-01:1080"))
            self.assertTrue(apply_block_verdicts(mixed))
            self.assertTrue(is_proxy_blocked("socks5h://wireproxy-01:1080"))
            self.assertFalse(is_proxy_blocked("socks5h://wireproxy-19:1080"))
        finally:
            for url in pool:
                clear_proxy_blocked(url)
                clear_proxy_exit_ip(url)

    def test_broken_block_probe_preserves_previous_verdict(self):
        urls = [f"socks5h://wireproxy-{i:02d}:1080" for i in range(1, 6)]
        mark_proxy_blocked(urls[0], 60)
        try:
            for i, url in enumerate(urls, start=1):
                set_proxy_exit_ip(url, f"198.51.100.{i}")
            with patch("proxy_health.set_proxy_control_verdict") as cache_verdict:
                self.assertFalse(apply_block_verdicts({url: True for url in urls}))
            cache_verdict.assert_not_called()
            self.assertTrue(is_proxy_blocked(urls[0]))
        finally:
            for url in urls:
                clear_proxy_blocked(url)
                clear_proxy_exit_ip(url)

    def test_block_probe_groups_shared_ipv4_and_requires_confirmation(self):
        p18 = "socks5h://wireproxy-18:1080"
        p19 = "socks5h://wireproxy-19:1080"
        p20 = "socks5h://wireproxy-20:1080"
        shared_ip = "192.0.2.18"
        served_ip = "192.0.2.19"
        try:
            set_proxy_exit_ip(p18, shared_ip)
            set_proxy_exit_ip(p20, shared_ip)
            set_proxy_exit_ip(p19, served_ip)
            verdicts = {p18: True, p20: True, p19: False}
            with patch("proxy_health.request_proxy_reconnect", return_value=False) as reconnect:
                self.assertTrue(apply_block_verdicts(verdicts))
                self.assertFalse(is_proxy_blocked(p18))
                self.assertFalse(is_proxy_blocked(p20))
                self.assertTrue(apply_block_verdicts(verdicts))
            self.assertEqual(
                {call.args[0] for call in reconnect.call_args_list}, {p18, p20}
            )
            self.assertTrue(is_proxy_blocked(p18))
            self.assertTrue(is_proxy_blocked(p20))
            self.assertFalse(is_proxy_blocked(p19))
        finally:
            record_exit_block_strike(shared_ip, False)
            record_exit_block_strike(served_ip, False)
            for url in (p18, p19, p20):
                clear_proxy_blocked(url)
                clear_proxy_exit_ip(url)

    def test_exit_ip_change_clears_old_block(self):
        proxy = "socks5h://wireproxy-18:1080"
        try:
            set_proxy_exit_ip(proxy, "104.28.1.1")
            mark_proxy_blocked(proxy, 60)
            self.assertTrue(record_probe_success(proxy, "104.28.2.2"))
            self.assertEqual(get_proxy_exit_ip(proxy), "104.28.2.2")
            self.assertFalse(is_proxy_blocked(proxy))
            self.assertFalse(record_probe_success(proxy, "104.28.2.2"))
        finally:
            clear_proxy_blocked(proxy)
            clear_proxy_exit_ip(proxy)

    def test_reconnect_signal_is_deduplicated_during_cooldown(self):
        proxy = "socks5h://wireproxy-99:1080"
        with tempfile.TemporaryDirectory() as signal_dir, \
                patch("proxy_state.RECONNECT_SIGNAL_DIR", signal_dir), \
                patch("proxy_state._redis_client", None):
            try:
                self.assertTrue(request_proxy_reconnect(proxy, cooldown_seconds=60))
                self.assertFalse(request_proxy_reconnect(proxy, cooldown_seconds=60))
                self.assertEqual(process_proxy_reconnect(proxy), "signaled")
                marker = f"{signal_dir}/wireproxy-99"
                with open(marker, encoding="ascii") as handle:
                    first_token = handle.read()
                self.assertFalse(request_proxy_reconnect(proxy, cooldown_seconds=60))
                with open(marker, encoding="ascii") as handle:
                    self.assertEqual(handle.read(), first_token)
                self.assertFalse(request_proxy_reconnect("socks5h://not-wireproxy:1080", 60))
            finally:
                clear_proxy_reconnect_state(proxy)

    def test_reconnect_waits_for_active_request_to_drain(self):
        proxy = "socks5h://wireproxy-97:1080"
        inc_in_flight(proxy)
        try:
            with tempfile.TemporaryDirectory() as signal_dir, \
                    patch("proxy_state.RECONNECT_SIGNAL_DIR", signal_dir), \
                    patch("proxy_state._redis_client", None):
                self.assertTrue(request_proxy_reconnect(
                    proxy,
                    cooldown_seconds=60,
                    drain_timeout_seconds=1,
                    drain_poll_seconds=0.01,
                ))
                marker = f"{signal_dir}/wireproxy-97"
                self.assertFalse(Path(marker).exists())
                self.assertFalse(is_proxy_usable(proxy))
                self.assertEqual(process_proxy_reconnect(proxy), "draining")
                dec_in_flight(proxy)
                self.assertEqual(process_proxy_reconnect(proxy), "signaled")
                self.assertTrue(Path(marker).exists())
        finally:
            dec_in_flight(proxy)
            clear_proxy_reconnect_state(proxy)

    def test_reconnect_is_postponed_after_drain_timeout(self):
        proxy = "socks5h://wireproxy-98:1080"
        inc_in_flight(proxy)
        try:
            with tempfile.TemporaryDirectory() as signal_dir, \
                    patch("proxy_state.RECONNECT_SIGNAL_DIR", signal_dir), \
                    patch("proxy_state._redis_client", None):
                self.assertTrue(request_proxy_reconnect(
                    proxy,
                    cooldown_seconds=60,
                    drain_timeout_seconds=0.05,
                    drain_poll_seconds=0.01,
                ))
                marker = f"{signal_dir}/wireproxy-98"
                time.sleep(0.12)
                self.assertFalse(Path(marker).exists())
                self.assertEqual(get_in_flight(proxy), 1)
                self.assertFalse(is_proxy_usable(proxy))
                import proxy_state
                proxy_state._set_until("drain_deadline", proxy, -1)
                self.assertEqual(process_proxy_reconnect(proxy), "postponed")
                self.assertFalse(Path(marker).exists())
                dec_in_flight(proxy)
                self.assertEqual(process_proxy_reconnect(proxy), "signaled")
                self.assertTrue(Path(marker).exists())
        finally:
            dec_in_flight(proxy)
            clear_proxy_reconnect_state(proxy)

    def test_reconnect_requires_verified_recovery(self):
        proxy = "socks5h://wireproxy-96:1080"
        with tempfile.TemporaryDirectory() as signal_dir, \
                patch("proxy_state.RECONNECT_SIGNAL_DIR", signal_dir), \
                patch("proxy_state._redis_client", None):
            try:
                set_proxy_exit_ip(proxy, "192.0.2.1")
                mark_proxy_blocked(proxy, 60)
                self.assertTrue(request_proxy_reconnect(proxy))
                self.assertEqual(process_proxy_reconnect(proxy), "signaled")
                self.assertEqual(process_proxy_reconnect(proxy), "verifying")
                record_probe_success(proxy, "192.0.2.2")
                with patch("proxy_state.RECONNECT_STABILIZE_SECONDS", 0):
                    self.assertEqual(process_proxy_reconnect(proxy), "stabilizing")
                    self.assertEqual(process_proxy_reconnect(proxy), "verified")
                self.assertTrue(is_proxy_usable(proxy))
            finally:
                clear_proxy_reconnect_state(proxy)
                clear_proxy_blocked(proxy)
                clear_proxy_exit_ip(proxy)

    def test_same_exit_ip_reconnect_is_not_counted_as_a_failure(self):
        import proxy_state

        proxy = "socks5h://wireproxy-93:1080"
        with tempfile.TemporaryDirectory() as signal_dir, \
                patch("proxy_state.RECONNECT_SIGNAL_DIR", signal_dir), \
                patch("proxy_state._redis_client", None), \
                patch("proxy_state.RECONNECT_BUDGET_LIMIT", 2), \
                patch("proxy_state.RECONNECT_BACKOFF_JITTER_SECONDS", 0):
            try:
                set_proxy_exit_ip(proxy, "192.0.2.10")
                # TikTok's block outlives the verify window, which is exactly what
                # makes a same-IP reconnect impossible to verify from inside it.
                mark_proxy_blocked(proxy, 600)
                self.assertTrue(request_proxy_reconnect(proxy))
                self.assertEqual(process_proxy_reconnect(proxy), "signaled")
                # The tunnel comes back, but on the very same public IPv4: most
                # exits cannot reroll at all, so this is the common outcome.
                record_probe_success(proxy, "192.0.2.10")
                self.assertEqual(process_proxy_reconnect(proxy), "verifying")
                proxy_state._clear("reconnecting", proxy)
                self.assertEqual(process_proxy_reconnect(proxy), "unchanged")

                state = proxy_state.reconnect_state(proxy)
                self.assertFalse(state["quarantined"])
                self.assertFalse(state["restart_scheduled"])
                self.assertEqual(state["restart_backoff_for_seconds"], 0)

                # Repeating it must never exhaust the budget: an exit that cannot
                # reroll would otherwise be quarantined for having working tunnels.
                for _ in range(3):
                    self.assertTrue(request_proxy_reconnect(proxy))
                    self.assertEqual(process_proxy_reconnect(proxy), "signaled")
                    record_probe_success(proxy, "192.0.2.10")
                    proxy_state._clear("reconnecting", proxy)
                    self.assertEqual(process_proxy_reconnect(proxy), "unchanged")
                self.assertFalse(proxy_state.reconnect_state(proxy)["quarantined"])
            finally:
                clear_proxy_reconnect_state(proxy)
                clear_proxy_blocked(proxy)
                clear_proxy_exit_ip(proxy)

    def test_failed_reconnects_backoff_then_quarantine(self):
        import proxy_state

        proxy = "socks5h://wireproxy-95:1080"
        with tempfile.TemporaryDirectory() as signal_dir, \
                patch("proxy_state.RECONNECT_SIGNAL_DIR", signal_dir), \
                patch("proxy_state._redis_client", None), \
                patch("proxy_state.RECONNECT_BUDGET_LIMIT", 2), \
                patch("proxy_state.RECONNECT_BACKOFF_JITTER_SECONDS", 0):
            try:
                self.assertTrue(request_proxy_reconnect(proxy))
                self.assertEqual(process_proxy_reconnect(proxy), "signaled")
                proxy_state._clear("reconnecting", proxy)
                self.assertEqual(process_proxy_reconnect(proxy), "verification_failed")
                self.assertGreater(
                    proxy_state.reconnect_state(proxy)["restart_backoff_for_seconds"], 0)
                self.assertFalse(is_proxy_usable(proxy))

                proxy_state._clear("reconnect_backoff", proxy)
                self.assertEqual(process_proxy_reconnect(proxy), "signaled")
                proxy_state._clear("reconnecting", proxy)
                self.assertEqual(process_proxy_reconnect(proxy), "quarantined")
                self.assertTrue(proxy_state.reconnect_state(proxy)["quarantined"])
                self.assertFalse(is_proxy_usable(proxy))
            finally:
                clear_proxy_reconnect_state(proxy)

    def test_every_reconnect_lifecycle_state_is_unusable(self):
        import proxy_state

        proxy = "socks5h://wireproxy-94:1080"
        try:
            for state in (
                "reconnect_pending",
                "reconnecting",
                "reconnect_backoff",
                "quarantine",
            ):
                proxy_state._set_until(state, proxy, 60)
                self.assertFalse(is_proxy_usable(proxy), state)
                clear_proxy_reconnect_state(proxy)
        finally:
            clear_proxy_reconnect_state(proxy)

    def test_monitor_fails_closed_for_reconnect_lifecycle(self):
        renderer_path = (
            Path(__file__).resolve().parent.parent
            / "yt-dlp-wireproxy"
            / "scripts"
            / "monitor-wireproxy-render.py"
        )
        spec = importlib.util.spec_from_file_location("monitor_wireproxy_render", renderer_path)
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)

        self.assertTrue(renderer.effective_usable({"usable": True}))
        for state in (
            {"restart_scheduled": True},
            {"draining": True},
            {"reconnecting": True},
            {"stabilizing": True},
            {"restart_backoff_for_seconds": 30},
            {"quarantined": True},
        ):
            self.assertFalse(renderer.effective_usable({"usable": True, **state}), state)

    def test_prometheus_metrics_include_proxy_runtime_state(self):
        import service_metrics

        with patch("service_metrics._client", None):
            service_metrics.inc_counter("test_tiktok_counter_total", result="ok")
            output = service_metrics.render_prometheus(12, [{
                "proxy": "socks5h://wireproxy-18:1080",
                "usable": True,
                "dead": False,
                "blocked": False,
                "draining": True,
                "reconnecting": False,
                "quarantined": False,
                "in_flight": 2,
            }])
        self.assertIn('test_tiktok_counter_total{result="ok"}', output)
        self.assertIn('tiktok_proxy_draining{proxy="socks5h://wireproxy-18:1080"} 1', output)
        self.assertIn('tiktok_proxy_in_flight{proxy="socks5h://wireproxy-18:1080"} 2', output)

    def test_background_reconnect_scheduler_has_managed_shutdown(self):
        import proxy_health

        async def exercise():
            proxy_health.start_prober()
            self.assertIsNotNone(proxy_health._reconnect_task)
            await proxy_health.stop_prober()
            self.assertIsNone(proxy_health._probe_task)
            self.assertIsNone(proxy_health._block_task)
            self.assertIsNone(proxy_health._reconnect_task)

        asyncio.run(exercise())

    def test_reconnect_scheduler_processes_all_providers(self):
        import proxy_health

        proxies = [
            "socks5h://wireproxy-01:1080",
            "socks5h://wireproxy-13:1080",
            "socks5h://wireproxy-18:1080",
        ]

        async def exercise():
            with patch("proxy_health.RECONNECT_SCHEDULER_INTERVAL_SECONDS", 0.01), \
                    patch("proxy_health._acquire_reconnect_lease", return_value=True), \
                    patch("proxy_health.get_proxy_pool", return_value=proxies), \
                    patch("proxy_health.process_proxy_reconnect") as process:
                task = asyncio.create_task(proxy_health._reconnect_scheduler_loop())
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(
                {call.args[0] for call in process.call_args_list},
                set(proxies),
            )

        asyncio.run(exercise())

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

        e4 = DummyErr("cannot complete SOCKS5 connection to www.tiktok.com")
        e4.code = 97
        self.assertIsInstance(_classify_error(e4), TikTokInfraError)

        ip_blocked = TikTokIPBlockedError()
        self.assertIsInstance(_classify_error(ip_blocked), TikTokIPBlockedError)

        restricted = TikTokAccessRestrictedError()
        self.assertIsInstance(_classify_error(restricted), TikTokAccessRestrictedError)
        self.assertFalse(restricted.retryable)

        generic = _classify_error(DummyErr("some parse error"))
        self.assertIsInstance(generic, TikTokExtractError)
        self.assertNotIsInstance(generic, TikTokInfraError)

    def test_generic_warp_failure_retries_via_indonesia(self):
        empty_shell = TikTokExtractError("empty shell")
        # Counts represent distinct known public IPv4 exits, not containers/IPv6.
        self.assertFalse(should_retry_via_indonesia("cloudflare", empty_shell, 0))
        self.assertFalse(should_retry_via_indonesia("cloudflare", empty_shell, 1))
        self.assertTrue(should_retry_via_indonesia("cloudflare", empty_shell, 2))
        self.assertFalse(should_retry_via_indonesia("geo", TikTokExtractError("parse error"), 2))
        self.assertFalse(should_retry_via_indonesia("cloudflare", TikTokInfraError("timeout"), 2))

    def test_repeated_10204_retries_via_indonesia(self):
        self.assertFalse(should_retry_ip_block_via_indonesia(0))
        self.assertFalse(should_retry_ip_block_via_indonesia(1))
        self.assertTrue(should_retry_ip_block_via_indonesia(2))

    def test_geo_hint_cache(self):
        key = "https://www.tiktok.com/@geo/video/987654321"
        with patch("session._redis_client", None):
            self.assertFalse(get_geo_hint(key))
            set_geo_hint(key, ttl=60)
            self.assertTrue(get_geo_hint(key))

    def _slideshow_session(self, status_code=200, body=b"\xff\xd8jpeg"):
        class FakeResponse:
            def __init__(self):
                self.status_code = status_code
                self.content = body

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, *_args, **_kwargs):
                return FakeResponse()

        return FakeSession()

    def test_failed_slideshow_render_raises_instead_of_empty_body(self):
        # ffmpeg failing used to reach the client as a 200 with a 0-byte mp4,
        # because the render ran inside the streaming generator after the
        # headers had already been sent.
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"Invalid data found"))

        with patch("main.AsyncSession", return_value=self._slideshow_session()), \
                patch("main.asyncio.create_subprocess_exec",
                      AsyncMock(return_value=proc)):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(render_slideshow(
                    ["https://cdn.example/1.jpg"], None, None, "chrome120"))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("render failed", caught.exception.detail.lower())

    def test_unreachable_slideshow_photo_is_attributed(self):
        with patch("main.AsyncSession",
                   return_value=self._slideshow_session(status_code=403)):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(render_slideshow(
                    ["https://cdn.example/1.jpg"], None, None, "chrome120"))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("photo 1", caught.exception.detail.lower())

    def test_successful_slideshow_render_hands_over_a_real_file(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))

        async def fake_exec(*cmd, **_kwargs):
            # ffmpeg's last argument is the output path; make it non-empty so the
            # size check passes the way a real render would.
            Path(cmd[-1]).write_bytes(b"\x00" * 32)
            return proc

        temp_dir = None
        try:
            with patch("main.AsyncSession", return_value=self._slideshow_session()), \
                    patch("main.asyncio.create_subprocess_exec", fake_exec):
                temp_dir, out_mp4 = asyncio.run(render_slideshow(
                    ["https://cdn.example/1.jpg"], None, None, "chrome120"))
            self.assertTrue(out_mp4.exists())
            self.assertEqual(out_mp4.stat().st_size, 32)

            chunks = []

            async def drain():
                async for chunk in stream_file_and_cleanup(temp_dir, out_mp4):
                    chunks.append(chunk)

            asyncio.run(drain())
            self.assertEqual(b"".join(chunks), b"\x00" * 32)
            # The generator owns cleanup, so the temp dir is gone once served.
            self.assertFalse(Path(temp_dir).exists())
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_control_verdict_cache_avoids_request_probe(self):
        with patch("main.get_proxy_control_verdict", return_value=False), \
                patch("proxy_health.probe_block", AsyncMock()) as live_probe:
            verdict = asyncio.run(verify_ambiguous_ip_block(
                "socks5h://wireproxy-18:1080"))
        self.assertIs(verdict, False)
        live_probe.assert_not_called()

    def test_10204_short_circuits_same_proxy_fallbacks(self):
        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        extractor = TikTokSSRExtractor(proxy="socks5h://wireproxy-18:1080")
        with patch("extractor.AsyncSession", return_value=FakeSession()), \
                patch.object(extractor, "resolve_url", AsyncMock(
                    return_value="https://www.tiktok.com/@geo/video/123")), \
                patch.object(extractor, "extract_via_web_ssr", AsyncMock(
                    side_effect=TikTokIPBlockedError())), \
                patch.object(extractor, "extract_via_embed_ssr", AsyncMock()) as embed, \
                patch.object(extractor, "extract_via_web_fallback", AsyncMock()) as fallback:
            with self.assertRaises(TikTokIPBlockedError):
                asyncio.run(extractor.extract("https://www.tiktok.com/@geo/video/123"))
        embed.assert_not_called()
        fallback.assert_not_called()

    def test_10204_geo_fallback_does_not_poison_proxy_pool(self):
        warp = "socks5h://wireproxy-18:1080"
        geo = "socks5h://wireproxy-02:1080"
        indo = "socks5h://wireproxy-01:1080"
        exit_ips = {
            warp: "104.28.197.9",
            geo: "138.199.60.173",
            indo: "93.185.162.26",
        }
        used = []

        class FakeExtractor:
            def __init__(self, proxy=None, impersonate=None):
                del impersonate
                self.proxy = proxy

            async def extract(self, _url):
                used.append(self.proxy)
                if self.proxy != indo:
                    raise TikTokIPBlockedError()
                return {"extract_source": "web_ssr", "title": "Indonesia success"}

        def pick_proxy(**kwargs):
            if kwargs.get("indo_only"):
                return indo
            if kwargs.get("warp_only"):
                return None if warp in kwargs.get("exclude", set()) else warp
            return geo

        request = MagicMock(method="POST")
        request.json = AsyncMock(return_value={
            "url": "https://www.tiktok.com/@vidiosports/video/7677284046552059143",
        })
        with patch("main.get_cached_extraction", return_value=None), \
                patch("main.get_geo_hint", return_value=False), \
                patch("main.get_next_proxy", side_effect=pick_proxy), \
                patch("main.get_proxy_exit_ip", side_effect=lambda p: exit_ips[p]), \
                patch("main.TikTokSSRExtractor", FakeExtractor), \
                patch("main.verify_ambiguous_ip_block", AsyncMock(return_value=False)), \
                patch("main.mark_proxy_and_shared_exit_blocked") as poison_pool, \
                patch("main.mark_proxy_request_success"), \
                patch("main.clear_proxy_and_shared_exit_blocked"), \
                patch("main.set_cached_extraction"), \
                patch("main.set_geo_hint") as geo_hint:
            response = asyncio.run(extract_tiktok(request))

        self.assertEqual(used, [warp, geo, indo])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["title"], "Indonesia success")
        poison_pool.assert_not_called()
        geo_hint.assert_called_once()

    def test_video_error_requires_two_healthy_indonesia_exits(self):
        proxies = [
            "socks5h://wireproxy-18:1080",
            "socks5h://wireproxy-02:1080",
            "socks5h://wireproxy-01:1080",
            "socks5h://wireproxy-13:1080",
        ]
        exits = {proxy: f"198.51.100.{i}" for i, proxy in enumerate(proxies, 1)}
        used = []

        class BlockedExtractor:
            def __init__(self, proxy=None, impersonate=None):
                del impersonate
                self.proxy = proxy

            async def extract(self, _url):
                used.append(self.proxy)
                raise TikTokIPBlockedError()

        def pick_proxy(**kwargs):
            excluded = kwargs.get("exclude", set())
            if kwargs.get("warp_only"):
                candidates = proxies[:1]
            elif kwargs.get("indo_only"):
                candidates = proxies[2:]
            else:
                candidates = proxies[1:2]
            return next((proxy for proxy in candidates if proxy not in excluded), None)

        request = MagicMock(method="POST")
        request.json = AsyncMock(return_value={
            "url": "https://www.tiktok.com/@geo/video/111222333",
        })
        with patch("main.get_cached_extraction", return_value=None), \
                patch("main.get_geo_hint", return_value=False), \
                patch("main.get_next_proxy", side_effect=pick_proxy), \
                patch("main.get_indo_proxies", return_value=proxies[2:]), \
                patch("main.get_proxy_exit_ip", side_effect=lambda p: exits[p]), \
                patch("main.TikTokSSRExtractor", BlockedExtractor), \
                patch("main.verify_ambiguous_ip_block", AsyncMock(return_value=False)):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(extract_tiktok(request))

        self.assertEqual(used, proxies)
        self.assertEqual(raised.exception.status_code, 422)

    def test_ip_block_fans_out_only_after_shared_ipv4_confirmation(self):
        p18 = "socks5h://wireproxy-18:1080"
        p19 = "socks5h://wireproxy-19:1080"
        p20 = "socks5h://wireproxy-20:1080"
        exits = {p18: "104.28.1.1", p19: "104.28.2.2", p20: "104.28.1.1"}
        with patch("main.get_proxy_pool", return_value=[p18, p19, p20]), \
                patch("main.get_proxy_exit_ip", side_effect=lambda p: exits[p]), \
                patch("main.mark_proxy_blocked") as mark_blocked, \
                patch("main.request_proxy_reconnect") as reconnect:
            try:
                first = mark_proxy_and_shared_exit_blocked(p18)
                second = mark_proxy_and_shared_exit_blocked(p20)
            finally:
                record_exit_block_strike(exits[p18], False)
        self.assertEqual(first, {p18})
        self.assertEqual(second, {p18, p20})
        marked = [call.args[0] for call in mark_blocked.call_args_list]
        reconnected = [call.args[0] for call in reconnect.call_args_list]
        self.assertEqual(marked.count(p18), 2)
        self.assertEqual(marked.count(p20), 1)
        self.assertCountEqual(reconnected, marked)

    def test_ip_block_reconnects_surfshark_and_mullvad(self):
        surfshark = "socks5h://wireproxy-01:1080"
        mullvad = "socks5h://wireproxy-13:1080"
        exits = {surfshark: "198.51.100.1", mullvad: "198.51.100.13"}
        with patch("main.get_proxy_pool", return_value=[surfshark, mullvad]), \
                patch("main.get_proxy_exit_ip", side_effect=lambda p: exits[p]), \
                patch("main.mark_proxy_blocked"), \
                patch("main.request_proxy_reconnect") as reconnect:
            try:
                mark_proxy_and_shared_exit_blocked(surfshark)
                mark_proxy_and_shared_exit_blocked(mullvad)
            finally:
                for exit_ip in exits.values():
                    record_exit_block_strike(exit_ip, False)
        self.assertEqual(
            {call.args[0] for call in reconnect.call_args_list},
            {surfshark, mullvad},
        )

    def test_tiktok_success_clears_block_for_shared_ipv4_siblings(self):
        p18 = "socks5h://wireproxy-18:1080"
        p19 = "socks5h://wireproxy-19:1080"
        p20 = "socks5h://wireproxy-20:1080"
        exits = {p18: "104.28.197.9", p19: "104.28.197.9", p20: "104.28.197.13"}
        with patch("main.get_proxy_pool", return_value=[p18, p19, p20]), \
                patch("main.get_proxy_exit_ip", side_effect=lambda p: exits[p]), \
                patch("main.clear_proxy_blocked") as clear_blocked:
            serving = clear_proxy_and_shared_exit_blocked(p19)
        self.assertEqual(serving, {p18, p19})
        self.assertEqual(
            {call.args[0] for call in clear_blocked.call_args_list},
            {p18, p19},
        )

    def test_liveness_probe_uses_fallback_and_shared_failure_counter(self):
        proxy = "socks5h://wireproxy-77:1080"

        class FakeResponse:
            status_code = 200
            text = "198.51.100.77"

        class FakeSession:
            calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("primary probe timed out")
                return FakeResponse()

        try:
            record_proxy_probe_failure(proxy, False)
            self.assertEqual(record_proxy_probe_failure(proxy, True), 1)
            self.assertEqual(record_proxy_probe_failure(proxy, True), 2)
            with patch("curl_cffi.requests.AsyncSession", return_value=FakeSession()):
                self.assertTrue(asyncio.run(probe_proxy(proxy)))
            self.assertEqual(get_proxy_exit_ip(proxy), "198.51.100.77")
            self.assertEqual(record_proxy_probe_failure(proxy, True), 1)
        finally:
            record_proxy_probe_failure(proxy, False)
            clear_proxy_exit_ip(proxy)
            mark_proxy_usable(proxy)

    def test_download_cooldown_only_for_transport_failures(self):
        self.assertTrue(should_cooldown_download_failure(TimeoutError("timed out")))
        self.assertFalse(should_cooldown_download_failure(ValueError("bad media metadata")))

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

    def test_warp_selection_deduplicates_shared_public_ipv4(self):
        import config
        pool = [f"socks5h://wireproxy-{i:02d}:1080" for i in (18, 19, 20)]
        exits = {pool[0]: "104.28.1.1", pool[1]: "104.28.1.1", pool[2]: "104.28.2.2"}
        with patch("config.get_proxy_exit_ips_many", return_value=exits):
            self.assertEqual(config._pick_from(pool, exclude_exit_ips={"104.28.1.1"}), pool[2])
            self.assertIsNone(config._pick_from(pool[:2], exclude_exit_ips={"104.28.1.1"}))

    def test_get_next_proxy_skips_dead(self):
        with patch("config.is_proxy_usable", side_effect=lambda p: "-01:" not in p):
            with patch("config.PROXY_COUNT", 5):
                for _ in range(20):
                    p = get_next_proxy()
                    self.assertIsNotNone(p)
                    self.assertNotIn("-01:", p)

    def test_get_next_proxy_least_loaded(self):
        with patch("config.PROXY_COUNT", 2):
            p_busy = "socks5h://wireproxy-01:1080"
            p_idle = "socks5h://wireproxy-02:1080"
            inc_in_flight(p_busy)
            with patch("config.random.random", return_value=0.5):
                for _ in range(10):
                    self.assertEqual(get_next_proxy(), p_idle)
            dec_in_flight(p_busy)


if __name__ == "__main__":
    unittest.main()
