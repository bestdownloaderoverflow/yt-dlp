"""Rate limiting utilities."""
from fastapi import Request, HTTPException
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import settings

# Create limiter instance
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{settings.RATE_LIMIT_PER_MINUTE} per minute"]
)


def setup_rate_limiting(app):
    """Setup rate limiting for FastAPI app."""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def rate_limit_check(request: Request, limit: str = "10/minute"):
    """Manual rate limit check."""
    # This is a simple implementation
    # For production, use Redis-based rate limiting
    pass
