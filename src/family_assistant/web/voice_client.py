"""
Voice client interfaces and implementations for Live Audio API.

This module wraps the Google GenAI Live API client. Since the google-genai
package is an optional dependency, we use TYPE_CHECKING imports for type
annotations and handle ImportError at runtime in the client class.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from google import genai
    from google.genai.live import AsyncSession
    from google.genai.types import LiveConnectConfigOrDict

logger = logging.getLogger(__name__)


class GoogleGeminiLiveClient:
    """Google Gemini Live API client.

    This is the only implementation - we use the SDK types directly rather than
    abstracting behind protocols, which allows full type checking.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self._client: genai.Client | None = None

    def _get_client(self) -> genai.Client:
        """Get or create the genai client (lazy initialization)."""
        if self._client is None:
            from google import (  # noqa: PLC0415 - google-genai is an optional dependency
                genai,
            )

            self._client = genai.Client(
                api_key=self.api_key, http_options={"api_version": "v1alpha"}
            )
        return self._client

    def connect(
        self,
        config: LiveConnectConfigOrDict | None,
    ) -> AbstractAsyncContextManager[AsyncSession]:
        """Connect to Gemini Live API.

        Returns an async context manager that yields an AsyncSession.
        Use with `async with` to establish the connection.

        Example:
            async with client.connect(config) as session:
                await session.send_realtime_input(audio={"data": audio_bytes, "mime_type": "audio/pcm"})
                async for response in session.receive():
                    ...
        """
        return self._get_client().aio.live.connect(model=self.model, config=config)


# Backward-compatible alias
LiveAudioClient = GoogleGeminiLiveClient
