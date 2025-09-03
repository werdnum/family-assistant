"""
Custom streaming mocks for LLM integration tests.

This module provides alternatives to VCR.py for testing streaming LLM responses,
specifically addressing the VCR.py issue #927 where MockStream doesn't implement
the readany() method required by aiohttp 3.12+ for streaming.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

logger = logging.getLogger(__name__)


class MockStreamReader:
    """Mock aiohttp StreamReader with proper streaming support."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Initialize with list of byte chunks to yield."""
        self.chunks = chunks
        self.index = 0
        self.closed = False

    async def readany(self) -> bytes:
        """Read any available data from the stream."""
        if self.index >= len(self.chunks) or self.closed:
            return b""

        chunk = self.chunks[self.index]
        self.index += 1

        # Add small delay to simulate streaming
        await asyncio.sleep(0.001)
        return chunk

    async def read(self, n: int = -1) -> bytes:
        """Read up to n bytes from the stream."""
        if self.index >= len(self.chunks) or self.closed:
            return b""

        chunk = self.chunks[self.index]
        self.index += 1
        return chunk

    def at_eof(self) -> bool:
        """Return True if we've reached end of stream."""
        return self.index >= len(self.chunks)

    async def readline(self) -> bytes:
        """Read a line from the stream."""
        return await self.readany()

    def close(self) -> None:
        """Close the stream."""
        self.closed = True


class MockClientResponse:
    """Mock aiohttp ClientResponse for streaming responses."""

    def __init__(
        self,
        chunks: list[bytes],
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize mock response."""
        self.chunks = chunks
        self.status = status
        self.headers = headers or {}
        self.content = MockStreamReader(chunks)
        self._closed = False

    async def __aenter__(self) -> "MockClientResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the response."""
        self._closed = True
        if hasattr(self.content, "close"):
            self.content.close()

    def raise_for_status(self) -> None:
        """Raise if status indicates an error."""
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")


def create_gemini_streaming_chunks(
    content: str, include_tool_calls: bool = False
) -> list[bytes]:
    """Create realistic Google Gemini streaming response chunks."""
    chunks = []

    # First chunk - usually contains metadata and start
    first_chunk = {
        "candidates": [
            {
                "content": {"parts": [{"text": ""}], "role": "model"},
                "finishReason": None,
                "index": 0,
                "safetyRatings": [
                    {
                        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "probability": "NEGLIGIBLE",
                    },
                    {
                        "category": "HARM_CATEGORY_HATE_SPEECH",
                        "probability": "NEGLIGIBLE",
                    },
                    {
                        "category": "HARM_CATEGORY_HARASSMENT",
                        "probability": "NEGLIGIBLE",
                    },
                    {
                        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                        "probability": "NEGLIGIBLE",
                    },
                ],
            }
        ]
    }
    chunks.append(json.dumps(first_chunk).encode() + b"\n")

    # Content chunks - split content into words
    words = content.split()
    for i, word in enumerate(words):
        text_content = word if i == 0 else f" {word}"
        chunk = {
            "candidates": [
                {
                    "content": {"parts": [{"text": text_content}], "role": "model"},
                    "finishReason": None,
                    "index": 0,
                }
            ]
        }
        chunks.append(json.dumps(chunk).encode() + b"\n")

    # Tool calls chunk if requested
    if include_tool_calls:
        tool_chunk = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "get_weather",
                                    "args": {"location": "Paris, France"},
                                }
                            }
                        ],
                        "role": "model",
                    },
                    "finishReason": None,
                    "index": 0,
                }
            ]
        }
        chunks.append(json.dumps(tool_chunk).encode() + b"\n")

    # Final chunk with completion and usage
    final_chunk = {
        "candidates": [
            {
                "content": {"parts": [{"text": ""}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 15,
            "candidatesTokenCount": 25,
            "totalTokenCount": 40,
        },
    }
    chunks.append(json.dumps(final_chunk).encode() + b"\n")

    return chunks


def create_gemini_error_chunks() -> list[bytes]:
    """Create error response chunks for testing error handling."""
    error_chunk = {
        "error": {
            "code": 400,
            "message": "Invalid request",
            "status": "INVALID_ARGUMENT",
        }
    }
    return [json.dumps(error_chunk).encode() + b"\n"]


class GeminiStreamingMocker:
    """Context manager for mocking Gemini streaming responses."""

    def __init__(self, mocker: Any) -> None:
        """Initialize with pytest-mock mocker."""
        self.mocker = mocker
        self.original_client_session = None
        self.mock_session = None

    def __enter__(self) -> "GeminiStreamingMocker":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def mock_basic_streaming_response(self, content: str = "1\n2\n3\n4\n5") -> None:
        """Mock a basic streaming response with the given content."""
        chunks = create_gemini_streaming_chunks(content)
        self._setup_mock_response(chunks)

    def mock_streaming_with_tool_calls(
        self, content: str = "Let me check the weather and do a calculation."
    ) -> None:
        """Mock a streaming response that includes tool calls."""
        chunks = create_gemini_streaming_chunks(content, include_tool_calls=True)
        self._setup_mock_response(chunks)

    def mock_streaming_error(self) -> None:
        """Mock a streaming response that returns an error."""
        chunks = create_gemini_error_chunks()
        self._setup_mock_response(chunks, status=400)

    def mock_system_message_response(self, content: str = "Paris") -> None:
        """Mock response for system message test."""
        chunks = create_gemini_streaming_chunks(content)
        self._setup_mock_response(chunks)

    def mock_multi_turn_response(
        self, content: str = "Your favorite color is blue."
    ) -> None:
        """Mock response for multi-turn conversation test."""
        chunks = create_gemini_streaming_chunks(content)
        self._setup_mock_response(chunks)

    def mock_reasoning_info_response(self, content: str = "hello world") -> None:
        """Mock response that includes reasoning/usage info."""
        chunks = create_gemini_streaming_chunks(content)
        self._setup_mock_response(chunks)

    def _setup_mock_response(self, chunks: list[bytes], status: int = 200) -> None:
        """Setup the mock aiohttp session with streaming response."""
        # Import here to avoid import issues

        # Create mock response
        mock_response = MockClientResponse(chunks, status)

        # Create mock session
        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        # Patch aiohttp.ClientSession
        self.mocker.patch("aiohttp.ClientSession", return_value=mock_session)

        # Also patch any other common import patterns
        self.mocker.patch("litellm.aiohttp.ClientSession", return_value=mock_session)

        logger.debug(
            f"Set up mock aiohttp session with {len(chunks)} chunks, status {status}"
        )


async def mock_gemini_streaming_response(
    content: str, include_tool_calls: bool = False
) -> AsyncIterator[bytes]:
    """Async generator that yields mock streaming chunks."""
    chunks = create_gemini_streaming_chunks(content, include_tool_calls)

    for chunk in chunks:
        yield chunk
        # Small delay to simulate streaming
        await asyncio.sleep(0.001)
