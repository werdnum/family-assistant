"""Camera backend support for Family Assistant.

This package provides a unified interface for interacting with camera systems
(Reolink, Frigate, etc.) through the CameraBackend protocol. It includes a fake
implementation for testing and optional real backend implementations.
"""

from __future__ import annotations

from family_assistant.camera.fake import FakeCameraBackend
from family_assistant.camera.protocol import (
    CameraBackend,
    CameraEvent,
    CameraInfo,
    FrameWithTimestamp,
    Recording,
)

# Conditionally import ReolinkBackend if reolink-aio is available
try:
    from family_assistant.camera.reolink import (
        ReolinkBackend,  # type: ignore[import-not-found]
        ReolinkCameraConfig,  # type: ignore[import-not-found]
        ReolinkCameraConfigDict,  # type: ignore[import-not-found]
        create_reolink_backend,  # type: ignore[import-not-found]
    )
except ImportError:
    ReolinkBackend = None  # type: ignore[assignment, misc]
    ReolinkCameraConfig = None  # type: ignore[assignment, misc]
    ReolinkCameraConfigDict = None  # type: ignore[assignment, misc]
    create_reolink_backend = None  # type: ignore[assignment, misc]

__all__ = [
    "CameraBackend",
    "CameraEvent",
    "CameraInfo",
    "FakeCameraBackend",
    "FrameWithTimestamp",
    "Recording",
    "ReolinkBackend",
    "ReolinkCameraConfig",
    "ReolinkCameraConfigDict",
    "create_reolink_backend",
]
