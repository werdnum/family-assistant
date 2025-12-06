"""
Voice client interfaces and implementations for Live Audio API.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager as AsyncContextManager
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class LiveAudioSession(Protocol):
    """Protocol for a live audio session."""

    # ast-grep-ignore: no-dict-any - Protocol definition needs flexibility
    async def send(
        self,
        *,
        input: Any,  # noqa: ANN401
        end_of_turn: bool | None = False,
    ) -> None:
        """Send data to the session."""
        ...

    # ast-grep-ignore: no-dict-any - Protocol definition needs flexibility
    def receive(self) -> AsyncIterator[Any]:  # noqa: ANN401
        """Receive data from the session."""
        ...


class LiveAudioClient(Protocol):
    """Protocol for a live audio client."""

    # ast-grep-ignore: no-dict-any - Protocol definition needs flexibility
    def connect(
        self,
        config: Any,  # noqa: ANN401
    ) -> AsyncContextManager[LiveAudioSession]:
        """Establish a connection."""
        ...


class GoogleGeminiLiveClient:
    """Google Gemini Live API client implementation."""

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    # ast-grep-ignore: no-dict-any - Protocol implementation needs flexibility
    def connect(
        self,
        config: Any,  # noqa: ANN401
    ) -> AsyncContextManager[LiveAudioSession]:
        """Connect to Gemini Live API."""
        try:
            from google import (  # noqa: PLC0415
                genai,
            )
        except ImportError as e:
            logger.error("google-genai package not found.")
            raise RuntimeError("google-genai package is required for voice mode") from e

        client = genai.Client(
            api_key=self.api_key, http_options={"api_version": "v1alpha"}
        )
        return client.aio.live.connect(model=self.model, config=config)
