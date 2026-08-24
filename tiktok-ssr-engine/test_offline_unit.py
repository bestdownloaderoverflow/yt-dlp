"""
Self-contained offline unit tests for tiktok-ssr-engine.
Does not require internet access, live servers, or proxies.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from config import get_proxy_pool
from extractor import (
    TikTokSSRExtractor,
    _decode_ssstik_url,
    _first_url,
    sanitize_filename_part,
    solve_slardar_challenge,
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
                if 1 <= num <= 11:
                    geo_indices.append(i)
            
            self.assertEqual(len(geo_indices), 11)
            max_gap = max(geo_indices[j+1] - geo_indices[j] for j in range(len(geo_indices)-1))
            self.assertLessEqual(max_gap, 5)


if __name__ == "__main__":
    unittest.main()
