"""Ephemeral in-memory store for live browser preview screenshots.

Holds at most one latest screenshot frame per active conversation or
subconversation so web and iOS clients can watch local and remote browser runs
live without persisting preview frames to disk or attachment storage.
"""

from __future__ import annotations

import io
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypedDict, runtime_checkable
from urllib.parse import quote

logger = logging.getLogger(__name__)

BrowserBackendKind = Literal["local", "remote"]

# Cap how many conversation/subconversation latest frames are kept in memory.
# Each entry holds one compressed screenshot (~50-200 KB), bounding RAM to a
# few tens of megabytes across the whole process.
DEFAULT_MAX_BROWSER_LIVE_VIEWS = 100

# Only transcode PNG screenshots above this size into JPEG so small PNGs (and
# tiny synthetic fixtures in tests) pass through untouched.
_JPEG_TRANSCODE_THRESHOLD_BYTES = 80_000
_JPEG_QUALITY = 82


class BrowserFrameEventPayload(TypedDict):
    """JSON payload carried by ``browser_frame`` SSE events."""

    type: Literal["browser_frame"]
    frame_id: str
    url: str
    captured_at: str
    tool_name: str | None
    backend_kind: BrowserBackendKind
    width: int | None
    height: int | None
    image_url: str


@dataclass(slots=True, frozen=True)
class BrowserLiveFrame:
    """Latest live screenshot captured for a conversation or subconversation."""

    frame_id: str
    image_bytes: bytes
    mime_type: str
    url: str
    captured_at: datetime
    tool_name: str | None
    backend_kind: BrowserBackendKind
    width: int | None
    height: int | None

    def to_payload(self, *, image_url: str) -> BrowserFrameEventPayload:
        """Build the wire-level metadata dict for SSE events and API responses."""
        return BrowserFrameEventPayload(
            type="browser_frame",
            frame_id=self.frame_id,
            url=self.url,
            captured_at=self.captured_at.isoformat(),
            tool_name=self.tool_name,
            backend_kind=self.backend_kind,
            width=self.width,
            height=self.height,
            image_url=image_url,
        )


def browser_view_frame_url(
    conversation_id: str,
    subconversation_id: str | None,
    frame_id: str,
) -> str:
    """Build the authenticated image URL for a live browser frame."""
    encoded_conv = quote(conversation_id, safe="")
    encoded_frame = quote(frame_id, safe="")
    if subconversation_id is None:
        return (
            f"/api/v1/chat/conversations/{encoded_conv}"
            f"/browser-view/frame?v={encoded_frame}"
        )
    encoded_sub = quote(subconversation_id, safe="")
    return (
        f"/api/v1/chat/conversations/{encoded_conv}"
        f"/subconversations/{encoded_sub}/browser-view/frame?v={encoded_frame}"
    )


def _optimize_frame_image(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Compress large PNG screenshots to JPEG for faster live streaming."""
    if mime_type != "image/png" or len(image_bytes) <= _JPEG_TRANSCODE_THRESHOLD_BYTES:
        return image_bytes, mime_type
    try:
        from PIL import Image  # noqa: PLC0415 - optional transcoding dependency
    except ImportError:
        return image_bytes, mime_type
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            rgb = img.convert("RGB")
            out = io.BytesIO()
            rgb.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            jpeg_bytes = out.getvalue()
    except Exception:
        logger.debug("Skipping JPEG transcoding for browser live frame", exc_info=True)
        return image_bytes, mime_type
    if len(jpeg_bytes) < len(image_bytes):
        return jpeg_bytes, "image/jpeg"
    return image_bytes, mime_type


class BrowserLiveViewStore:
    """Bounded in-memory LRU cache of the latest browser frame per stream target."""

    def __init__(self, *, max_entries: int = DEFAULT_MAX_BROWSER_LIVE_VIEWS) -> None:
        self._max_entries = max(1, max_entries)
        self._frames: OrderedDict[tuple[str, str | None], BrowserLiveFrame] = (
            OrderedDict()
        )
        self._next_frame_id = 1

    def set_frame(
        self,
        *,
        conversation_id: str,
        subconversation_id: str | None,
        image_bytes: bytes,
        mime_type: str,
        url: str,
        tool_name: str | None,
        backend_kind: BrowserBackendKind,
        width: int | None,
        height: int | None,
        captured_at: datetime | None = None,
    ) -> BrowserLiveFrame:
        """Store and return the latest frame for ``(conversation_id, subconversation_id)``."""
        optimized_bytes, optimized_mime = _optimize_frame_image(image_bytes, mime_type)
        frame_id = str(self._next_frame_id)
        self._next_frame_id += 1
        frame = BrowserLiveFrame(
            frame_id=frame_id,
            image_bytes=optimized_bytes,
            mime_type=optimized_mime,
            url=url,
            captured_at=captured_at or datetime.now(UTC),
            tool_name=tool_name,
            backend_kind=backend_kind,
            width=width,
            height=height,
        )
        key = (conversation_id, subconversation_id)
        self._frames[key] = frame
        self._frames.move_to_end(key)
        while len(self._frames) > self._max_entries:
            self._frames.popitem(last=False)
        return frame

    def get_frame(
        self,
        conversation_id: str,
        subconversation_id: str | None = None,
    ) -> BrowserLiveFrame | None:
        """Return the latest stored frame for the target, or ``None``."""
        key = (conversation_id, subconversation_id)
        frame = self._frames.get(key)
        if frame is not None:
            self._frames.move_to_end(key)
        return frame

    def clear_frame(
        self,
        conversation_id: str,
        subconversation_id: str | None = None,
    ) -> None:
        """Remove any stored frame for the target."""
        self._frames.pop((conversation_id, subconversation_id), None)


@runtime_checkable
class BrowserLiveViewPublisher(Protocol):
    """Sink that stores a live browser frame and notifies streaming subscribers."""

    async def publish_browser_frame(
        self,
        *,
        conversation_id: str,
        subconversation_id: str | None,
        turn_id: str | None,
        image_bytes: bytes,
        mime_type: str,
        url: str,
        tool_name: str | None,
        backend_kind: BrowserBackendKind,
        width: int | None,
        height: int | None,
        captured_at: datetime | None = None,
    ) -> BrowserLiveFrame: ...
