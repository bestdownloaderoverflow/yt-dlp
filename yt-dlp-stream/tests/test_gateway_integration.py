import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request


RUN_INTEGRATION = os.getenv("RUN_GATEWAY_INTEGRATION", "").strip().lower() in {"1", "true", "yes"}
BASE_URL = os.getenv("GATEWAY_BASE_URL", "http://localhost:9111").rstrip("/")
YOUTUBE_URL = os.getenv("TEST_YOUTUBE_URL", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
TIKTOK_URL = os.getenv(
    "TEST_TIKTOK_URL",
    "https://www.tiktok.com/@yusuf_sufiandi24/photo/7457053391559216392",
)


def _http_json(method: str, path: str, timeout: int = 120, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, dict(resp.headers.items()), json.loads(body.decode("utf-8"))


def _http_stream(method: str, path: str, timeout: int = 120, headers: dict | None = None):
    req = urllib.request.Request(f"{BASE_URL}{path}", method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Read a small chunk to ensure response body actually starts flowing.
        sample = resp.read(1024)
        return resp.status, dict(resp.headers.items()), sample


@unittest.skipUnless(RUN_INTEGRATION, "Set RUN_GATEWAY_INTEGRATION=1 to run live gateway integration tests")
class GatewayIntegrationTests(unittest.TestCase):
    def test_health(self):
        status, _, payload = _http_json("GET", "/health", timeout=20)
        self.assertEqual(status, 200)
        self.assertIn(payload.get("status"), {"healthy", "degraded"})
        self.assertIsInstance(payload.get("workers"), list)
        self.assertGreaterEqual(len(payload.get("workers")), 1)

    def test_youtube_fetch_and_download(self):
        q = urllib.parse.quote(YOUTUBE_URL, safe="")
        status, _, payload = _http_json("GET", f"/fetch?url={q}", timeout=120)
        self.assertEqual(status, 200)

        links = payload.get("download_links", {})
        video_links = links.get("video", {})
        key_link = video_links.get("360p") or next(iter(video_links.values()))
        self.assertTrue(key_link, "missing video download key")
        key = key_link.split("key=", 1)[-1]

        dl_status, dl_headers, dl_sample = _http_stream(
            "GET",
            f"/download?key={urllib.parse.quote(key, safe='')}",
            timeout=120,
            headers={"Range": "bytes=0-1023"},
        )
        self.assertIn(dl_status, {200, 206})
        self.assertTrue(dl_headers.get("Content-Type", "").startswith("video/"))
        self.assertGreater(len(dl_sample), 0)

    def test_stream_endpoints(self):
        q = urllib.parse.quote(YOUTUBE_URL, safe="")
        cases = [
            (f"/stream/video?url={q}&quality=360", "video/"),
            (f"/stream/video-chunked?url={q}&quality=360", "video/"),
            (f"/stream/mp3-chunked?url={q}", "audio/mpeg"),
            (f"/stream/m4a?url={q}", "audio/mp4"),
        ]
        for path, expected_ct in cases:
            with self.subTest(path=path):
                status, headers, sample = _http_stream(
                    "GET",
                    path,
                    timeout=120,
                    headers={"Range": "bytes=0-1023"},
                )
                self.assertIn(status, {200, 206})
                content_type = headers.get("Content-Type", "")
                self.assertTrue(
                    content_type.startswith(expected_ct),
                    f"unexpected content-type for {path}: {content_type}",
                )
                self.assertGreater(len(sample), 0)

    def test_tiktok_flow(self):
        payload = json.dumps({"url": TIKTOK_URL}).encode("utf-8")
        status, _, data = _http_json(
            "POST",
            "/tiktok",
            timeout=120,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        slideshow = data.get("download_slideshow", "")
        self.assertIn("key=", slideshow)
        key = slideshow.split("key=", 1)[-1]

        dl_status, dl_headers, dl_sample = _http_stream(
            "GET",
            f"/tiktok/download?key={urllib.parse.quote(key, safe='')}",
            timeout=120,
        )
        self.assertEqual(dl_status, 200)
        self.assertTrue(dl_headers.get("Content-Type", "").startswith("video/mp4"))
        self.assertGreater(len(dl_sample), 0)

