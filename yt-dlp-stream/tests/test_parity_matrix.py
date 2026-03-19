import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

if "redis" not in sys.modules:
    class _FakeRedisConn:
        def __init__(self):
            self._store = {}

        def setex(self, key, ttl, value):
            self._store[key] = value

        def get(self, key):
            return self._store.get(key)

        def delete(self, key):
            self._store.pop(key, None)

        def ping(self):
            return True

    class _FakeRedisModule:
        class ConnectionError(Exception):
            pass

        @staticmethod
        def from_url(url, decode_responses=True):
            return _FakeRedisConn()

    sys.modules["redis"] = _FakeRedisModule()

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from api import stream_chunked as stream_chunked_api
from api import download as download_api
from core import ffmpeg as ffmpeg_core
from core import helpers
from core import generators


class _DummyRequest:
    def __init__(self, headers=None, host="127.0.0.1"):
        self.headers = headers or {}
        self.client = types.SimpleNamespace(host=host)
        self._disconnected = False

    async def is_disconnected(self):
        return self._disconnected


class _DummyResponse:
    def __init__(self, status_code=206, content_range="bytes 0-9/10"):
        self.status_code = status_code
        self.headers = {"Content-Range": content_range} if content_range else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPException(status_code=self.status_code, detail="http error")

    async def aiter_bytes(self, chunk_size=65536):
        yield b"1234567890"


class _DummyStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DummyAsyncClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, headers=None):
        return _DummyStreamCM(self._response)


class ParityMatrixTests(unittest.IsolatedAsyncioTestCase):
    def test_client_id_prefers_x_forwarded_for(self):
        request = _DummyRequest(headers={"x-forwarded-for": "10.10.10.1, 172.17.0.1", "user-agent": "UA"})
        client_id = helpers._client_id_from_request(request)
        self.assertEqual(client_id, "10.10.10.1:UA")

    async def test_stream_video_chunked_strict_multi_progressive_routes_to_ffmpeg(self):
        request = _DummyRequest()
        info = {"title": "demo", "duration": 10}
        resolved = [{"url": "https://v", "vcodec": "avc1"}, {"url": "https://a", "vcodec": "none", "acodec": "mp4a"}]

        with (
            mock.patch.object(stream_chunked_api, "_enforce_rate_limit"),
            mock.patch.object(stream_chunked_api, "_extract_info_and_resolve", return_value=(info, resolved, {})),
            mock.patch.object(stream_chunked_api, "plan_delivery", return_value=types.SimpleNamespace(mode="multi_progressive", reason="test")),
            mock.patch.object(stream_chunked_api, "_streaming_response", new=mock.AsyncMock(return_value="ffmpeg-path")),
        ):
            result = await stream_chunked_api.stream_video_chunked(
                url="https://example.com/video",
                strict=True,
                request=request,
            )
        self.assertEqual(result, "ffmpeg-path")

    async def test_stream_video_chunked_single_progressive_uses_format_ext_mime(self):
        request = _DummyRequest()
        info = {"title": "demo", "duration": 10}
        resolved = [{"url": "https://v", "ext": "webm", "filesize": 123, "http_headers": {"User-Agent": "UA"}}]

        with (
            mock.patch.object(stream_chunked_api, "_enforce_rate_limit"),
            mock.patch.object(stream_chunked_api, "_extract_info_and_resolve", return_value=(info, resolved, {})),
            mock.patch.object(stream_chunked_api, "plan_delivery", return_value=types.SimpleNamespace(mode="single_progressive", reason="test")),
        ):
            response = await stream_chunked_api.stream_video_chunked(
                url="https://example.com/video",
                strict=True,
                request=request,
            )

        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.media_type, "video/webm")
        self.assertIn("filename=\"demo.webm\"", response.headers.get("content-disposition", ""))

    async def test_stream_video_chunked_single_progressive_ios_unsafe_routes_to_ffmpeg(self):
        request = _DummyRequest()
        info = {"title": "demo", "duration": 10}
        resolved = [{
            "url": "https://v",
            "ext": "mp4",
            "vcodec": "av01.0.08M.08",
            "acodec": "mp4a.40.2",
            "filesize": 123,
            "http_headers": {"User-Agent": "UA"},
        }]

        with (
            mock.patch.object(stream_chunked_api, "_enforce_rate_limit"),
            mock.patch.object(stream_chunked_api, "_extract_info_and_resolve", return_value=(info, resolved, {})),
            mock.patch.object(stream_chunked_api, "plan_delivery", return_value=types.SimpleNamespace(mode="single_progressive", reason="test")),
            mock.patch.object(stream_chunked_api, "_streaming_response", new=mock.AsyncMock(return_value="ffmpeg-path")),
        ):
            response = await stream_chunked_api.stream_video_chunked(
                url="https://example.com/video",
                strict=True,
                request=request,
            )

        self.assertEqual(response, "ffmpeg-path")

    async def test_download_video_strict_multi_progressive_routes_to_ffmpeg(self):
        request = _DummyRequest()
        info = {"title": "demo"}
        resolved = [{"url": "https://v", "vcodec": "avc1"}, {"url": "https://a", "vcodec": "none", "acodec": "mp4a"}]

        with (
            mock.patch.object(download_api, "_extract_info_and_resolve", return_value=(info, resolved, {})),
            mock.patch.object(download_api, "plan_delivery", return_value=types.SimpleNamespace(mode="multi_progressive", reason="test")),
            mock.patch.object(download_api, "_streaming_response", new=mock.AsyncMock(return_value="ffmpeg-path")),
        ):
            result = await download_api._download_video(
                url="https://example.com/video",
                quality="1080",
                download=True,
                request=request,
                strict=True,
            )
        self.assertEqual(result, "ffmpeg-path")

    async def test_download_video_single_progressive_ios_unsafe_routes_to_ffmpeg(self):
        request = _DummyRequest()
        info = {"title": "demo"}
        resolved = [{
            "url": "https://v",
            "ext": "mp4",
            "vcodec": "vp9.00.51.08",
            "acodec": "opus",
            "filesize": 123,
            "http_headers": {"User-Agent": "UA"},
        }]

        with (
            mock.patch.object(download_api, "_extract_info_and_resolve", return_value=(info, resolved, {})),
            mock.patch.object(download_api, "plan_delivery", return_value=types.SimpleNamespace(mode="single_progressive", reason="test")),
            mock.patch.object(download_api, "_streaming_response", new=mock.AsyncMock(return_value="ffmpeg-path")),
        ):
            result = await download_api._download_video(
                url="https://example.com/video",
                quality="1080",
                download=True,
                request=request,
                strict=False,
            )
        self.assertEqual(result, "ffmpeg-path")

    async def test_download_mp3_strict_routes_to_ffmpeg(self):
        request = _DummyRequest()
        info = {"title": "demo"}
        resolved = [{"url": "https://a", "ext": "m4a"}]

        with (
            mock.patch.object(download_api, "_extract_info_and_resolve", return_value=(info, resolved, {})),
            mock.patch.object(download_api, "plan_delivery", return_value=types.SimpleNamespace(mode="single_progressive", reason="test")),
            mock.patch.object(download_api, "_streaming_response", new=mock.AsyncMock(return_value="ffmpeg-path")),
        ):
            result = await download_api._download_mp3(
                url="https://example.com/video",
                download=True,
                request=request,
                strict=True,
            )
        self.assertEqual(result, "ffmpeg-path")

    async def test_chunked_generator_rejects_invalid_content_range(self):
        bad_response = _DummyResponse(status_code=206, content_range="bytes 5-9/10")

        def _client_factory(*args, **kwargs):
            return _DummyAsyncClient(bad_response)

        with mock.patch.object(generators.httpx, "AsyncClient", side_effect=_client_factory):
            agen = generators._chunked_video_generator(
                url="https://example.com/video",
                headers={"User-Agent": "UA"},
                total_size=10,
                chunk_size=10,
                ydl_refresh_info={"url": "x", "format_str": "best", "ydl_opts": {}},
            )
            with self.assertRaises(HTTPException) as exc:
                await anext(agen)
            self.assertEqual(exc.exception.status_code, 502)
            self.assertIn("Invalid Content-Range", str(exc.exception.detail))

    def test_ffmpeg_single_transcodes_non_ios_codec(self):
        cmd = ffmpeg_core.build_ffmpeg_single_cmd(
            fmt={"url": "https://v", "vcodec": "av01.0.08M.08", "acodec": "opus"},
            global_headers={},
            audio_only=False,
        )
        self.assertIn("libx264", cmd)
        self.assertIn("aac", cmd)

    def test_ffmpeg_merge_keeps_copy_for_avc1(self):
        cmd = ffmpeg_core.build_ffmpeg_merge_cmd(
            video_fmt={"url": "https://v", "vcodec": "avc1.640028"},
            audio_fmt={"url": "https://a", "acodec": "mp4a.40.2"},
            global_headers={},
        )
        self.assertIn("copy", cmd)
        self.assertNotIn("libx264", cmd)

    def test_ffmpeg_merge_transcodes_non_ios_codec(self):
        cmd = ffmpeg_core.build_ffmpeg_merge_cmd(
            video_fmt={"url": "https://v", "vcodec": "vp9.00.51.08"},
            audio_fmt={"url": "https://a", "acodec": "opus"},
            global_headers={},
        )
        self.assertIn("libx264", cmd)


if __name__ == "__main__":
    unittest.main()
