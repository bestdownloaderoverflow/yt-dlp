"""
Internal tunnel system untuk two-tier streaming.
Hanya accessible dari localhost - digunakan oleh ffmpeg dan chunked downloader.
"""
import asyncio
import re
import secrets
from typing import Dict, Optional, Callable, AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
import httpx


_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")


@dataclass
class InternalStreamInfo:
    """Stream information stored untuk internal tunnel."""
    url: str
    headers: Dict[str, str]
    service: str  # youtube, tiktok, etc.
    created_at: datetime = field(default_factory=datetime.now)
    transplant_func: Optional[Callable] = None
    chunk_count: int = 0
    last_refresh_chunk: int = 0


class InternalTunnelManager:
    """
    Manager untuk internal tunnels.

    Mirip Cobalt's /itunnel - hanya accessible dari localhost.
    """

    # Refresh URL setiap 3 chunks (24MB untuk 8MB chunks)
    CHUNKS_BEFORE_REFRESH = 3
    CHUNK_SIZE = 8 * 1024 * 1024  # 8MB

    def __init__(self):
        self._streams: Dict[str, InternalStreamInfo] = {}
        self._cleanup_interval = 300  # 5 minutes

    def create_stream(
        self,
        url: str,
        headers: Dict[str, str],
        service: str = "generic",
        transplant_func: Optional[Callable] = None,
    ) -> str:
        """
        Create internal stream dan return stream ID.

        Returns:
            stream_id: Unique identifier untuk stream ini
        """
        stream_id = secrets.token_urlsafe(16)
        self._streams[stream_id] = InternalStreamInfo(
            url=url,
            headers=headers,
            service=service,
            transplant_func=transplant_func,
        )
        return stream_id

    def get_stream(self, stream_id: str) -> Optional[InternalStreamInfo]:
        """Get stream info by ID."""
        return self._streams.get(stream_id)

    async def refresh_stream_url(self, stream_id: str) -> bool:
        """
        Refresh expired URL menggunakan transplant function.

        Returns:
            True jika refresh berhasil, False otherwise
        """
        stream = self._streams.get(stream_id)
        if not stream or not stream.transplant_func:
            return False

        try:
            new_url = await stream.transplant_func()
            if new_url:
                stream.url = new_url
                stream.last_refresh_chunk = stream.chunk_count
                return True
        except Exception as e:
            print(f"Failed to refresh URL: {e}")

        return False

    async def read_chunks(
        self, stream_id: str, total_size: int
    ) -> AsyncIterator[bytes]:
        """
        Cobalt-style chunked download dengan URL refresh.

        Download dalam 8MB chunks dengan fresh connection per chunk.
        Refresh URL setiap 3 chunks atau jika mendapat 403 error.
        """
        stream = self._streams.get(stream_id)
        if not stream:
            raise ValueError(f"Stream {stream_id} not found")

        read = 0
        refresh_count = 0
        max_refreshes = 10

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            while read < total_size:
                # Check if need refresh (every 3 chunks)
                chunks_since_refresh = (
                    stream.chunk_count - stream.last_refresh_chunk
                )
                if chunks_since_refresh >= self.CHUNKS_BEFORE_REFRESH:
                    await self.refresh_stream_url(stream_id)

                end = min(read + self.CHUNK_SIZE - 1, total_size - 1)
                range_headers = {**stream.headers, "Range": f"bytes={read}-{end}"}

                try:
                    async with client.stream(
                        "GET", stream.url, headers=range_headers
                    ) as resp:
                        if resp.status_code == 416:
                            if read >= total_size:
                                break
                            raise Exception(
                                f"Range not satisfiable at {read}/{total_size}"
                            )

                        # Handle 403 - URL expired
                        if (
                            resp.status_code == 403
                            and refresh_count < max_refreshes
                        ):
                            success = await self.refresh_stream_url(stream_id)
                            if success:
                                refresh_count += 1
                                continue  # Retry dengan URL baru
                            else:
                                raise Exception(
                                    "URL expired and refresh failed"
                                )

                        resp.raise_for_status()

                        if resp.status_code == 206:
                            content_range = resp.headers.get("Content-Range")
                            match = _CONTENT_RANGE_RE.match(content_range.strip()) if content_range else None
                            if not match or int(match.group(1)) != read:
                                raise Exception(
                                    f"Invalid Content-Range at {read}: {content_range}"
                                )
                        elif resp.status_code == 200 and read > 0:
                            raise Exception(
                                "Server ignored Range mid-download; cannot continue safely"
                            )

                        async for chunk in resp.aiter_bytes():
                            yield chunk
                            read += len(chunk)

                        stream.chunk_count += 1

                except httpx.HTTPStatusError as e:
                    if (
                        e.response.status_code == 403
                        and refresh_count < max_refreshes
                    ):
                        success = await self.refresh_stream_url(stream_id)
                        if success:
                            refresh_count += 1
                            continue
                    raise

    def destroy_stream(self, stream_id: str):
        """Cleanup stream dari memory."""
        self._streams.pop(stream_id, None)

    def cleanup_expired(self, max_age_seconds: int = 3600):
        """Remove expired streams."""
        now = datetime.now()
        expired = [
            sid
            for sid, stream in self._streams.items()
            if (now - stream.created_at).total_seconds() > max_age_seconds
        ]
        for sid in expired:
            self.destroy_stream(sid)


# Global instance
internal_tunnel = InternalTunnelManager()
