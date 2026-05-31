"""Hybrid media delivery helpers.

This module keeps yt-dlp focused on extraction/resolution, while the HTTP layer
decides how bytes should be delivered to the client:
- progressive HTTP URLs -> direct/chunked delivery
- split progressive audio/video -> chunked merge
- HLS/DASH/manifests -> ffmpeg live pipe
"""

from dataclasses import dataclass
from typing import Literal

from core.helpers import can_use_chunked_streaming, can_use_chunked_streaming_multi


DeliveryMode = Literal["single_progressive", "multi_progressive", "ffmpeg"]


@dataclass(frozen=True)
class DeliveryPlan:
    """How a resolved yt-dlp format selection should be delivered."""

    mode: DeliveryMode
    reason: str


def plan_delivery(resolved: list[dict]) -> DeliveryPlan:
    """Pick a delivery strategy from resolved yt-dlp formats."""
    if len(resolved) == 1 and can_use_chunked_streaming(resolved[0]):
        return DeliveryPlan(
            mode="single_progressive",
            reason="single progressive URL; direct/chunked HTTP delivery is supported",
        )

    if len(resolved) == 2 and can_use_chunked_streaming_multi(resolved):
        return DeliveryPlan(
            mode="multi_progressive",
            reason="separate progressive audio/video URLs; chunked merge can be used",
        )

    return DeliveryPlan(
        mode="ffmpeg",
        reason="manifest/remux processing is required; ffmpeg live pipe is needed",
    )
