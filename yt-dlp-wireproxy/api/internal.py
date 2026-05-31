"""Internal tunnel endpoints for secure internal streaming."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse

from internal_tunnel import internal_tunnel
from security import is_localhost, validate_stream_token

router = APIRouter()
logger = logging.getLogger("ytdl_stream")


@router.get("/_internal")
async def internal_tunnel_endpoint(
    id: str = Query(..., description="Stream ID"),
    expires: float = Query(..., description="Token expiration timestamp"),
    sig: str = Query(..., description="HMAC signature"),
    request: FastAPIRequest = None,
):
    """
    Internal tunnel endpoint - hanya accessible dari localhost dengan valid signature.

    Digunakan oleh ffmpeg dan chunked downloader untuk akses stream
    dengan headers/cookies yang benar.

    Security:
    - Localhost only
    - HMAC signature validation
    - Token expiration check
    """
    # Security: hanya allow localhost
    client_host = request.client.host if request.client else ""
    if not is_localhost(client_host):
        logger.warning(f"Forbidden access to internal endpoint from {client_host}")
        raise HTTPException(status_code=403, detail="Forbidden")

    # Validate signature & expiration
    is_valid, error = validate_stream_token(id, expires, sig)
    if not is_valid:
        logger.warning(f"Invalid stream token for {id}: {error}")
        raise HTTPException(status_code=401, detail=error)

    stream = internal_tunnel.get_stream(id)
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Return stream info untuk ffmpeg/internal use
    return {
        "url": stream.url,
        "headers": stream.headers,
        "service": stream.service,
    }


@router.get("/_internal/chunked")
async def internal_chunked_endpoint(
    id: str = Query(..., description="Stream ID"),
    size: int = Query(..., description="Total file size"),
    expires: float = Query(..., description="Token expiration timestamp"),
    sig: str = Query(..., description="HMAC signature"),
    request: FastAPIRequest = None,
):
    """
    Internal chunked download endpoint dengan security.

    Streams data dalam chunks dengan URL refresh otomatis.

    Security:
    - Localhost only
    - HMAC signature validation
    - Token expiration check
    """
    client_host = request.client.host if request.client else ""
    if not is_localhost(client_host):
        logger.warning(f"Forbidden access to internal chunked endpoint from {client_host}")
        raise HTTPException(status_code=403, detail="Forbidden")

    # Validate signature & expiration
    is_valid, error = validate_stream_token(id, expires, sig)
    if not is_valid:
        logger.warning(f"Invalid stream token for chunked {id}: {error}")
        raise HTTPException(status_code=401, detail=error)

    async def chunk_generator():
        try:
            async for chunk in internal_tunnel.read_chunks(id, size):
                yield chunk
        finally:
            # Cleanup setelah selesai
            internal_tunnel.destroy_stream(id)

    return StreamingResponse(
        chunk_generator(), media_type="application/octet-stream"
    )
