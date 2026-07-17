"""The Google data-API seam used by the Gmail/Drive tools (later wave).

Mirrors the injectable-backend pattern of ``tools/video_backends.py``: a single
generic ``request`` method rather than per-endpoint methods keeps the protocol
stable as tools grow. The tools own building full request URLs
(``https://gmail.googleapis.com/...`` / ``https://www.googleapis.com/drive/v3/...``)
and interpreting the response; this module only carries an access token to the
wire as a Bearer header and returns the raw status/bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

# Default per-request timeout for Google REST calls (seconds).
_DEFAULT_TIMEOUT_SECONDS = 30.0

# Hard ceiling on a response body read from a Google REST endpoint (bytes). The
# body is streamed and aborted once this budget is exceeded, so a multi-gigabyte
# Drive download (``alt=media``) or a hostile export can never be materialized in
# memory. 25 MiB matches the Drive download pre-check in ``tools/google_data.py``.
_DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024


class GoogleApiError(Exception):
    """Raised on a transport-level failure talking to a Google REST endpoint.

    HTTP error *statuses* are returned to the caller in
    :class:`GoogleApiResponse` (the tools interpret them); this is reserved for
    failures where no response was received (network error, timeout).
    """


@dataclass(frozen=True)
class GoogleApiResponse:
    """A minimal Google REST response: status code and raw body bytes."""

    status_code: int
    content: bytes

    def json(self) -> Any:  # noqa: ANN401 - arbitrary decoded JSON shape
        """Decode the body as JSON."""
        return json.loads(self.content)


@runtime_checkable
class GoogleApiBackend(Protocol):
    """Protocol for the Google REST backend used by the Gmail/Drive tools.

    Implementations MUST enforce a hard response-body cap while reading, raising
    :class:`GoogleApiError` when a body exceeds it, so an unbounded download
    (e.g. Drive ``alt=media`` on a huge file) can never exhaust server memory.
    """

    async def request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None = None,
    ) -> GoogleApiResponse:
        """Issue an authenticated request to a full Google REST URL."""
        ...


class HttpGoogleApiBackend:
    """A :class:`GoogleApiBackend` over an injected ``httpx.AsyncClient``."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        """Initialize with an async HTTP client, timeout, and response-body cap."""
        self._http_client = http_client
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    async def request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None = None,
    ) -> GoogleApiResponse:
        """Issue an authenticated request, returning status and raw bytes.

        The access token is sent as a Bearer ``Authorization`` header and is
        never logged. The response body is streamed and read against a hard
        ``max_response_bytes`` budget so an unbounded download can never be
        materialized in memory; exceeding it raises :class:`GoogleApiError`.
        Transport-level failures also raise :class:`GoogleApiError`; HTTP error
        statuses are returned for the caller to interpret.
        """
        try:
            return await self._stream_request(
                method=method, url=url, access_token=access_token, params=params
            )
        except httpx.HTTPError as exc:
            raise GoogleApiError(f"Google API request to {url} failed") from exc

    async def _stream_request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None,
    ) -> GoogleApiResponse:
        """Stream the response body under the ``max_response_bytes`` budget."""
        async with self._http_client.stream(
            method,
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=dict(params) if params is not None else None,
            timeout=self._timeout,
        ) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise GoogleApiError(
                        f"Google API response from {url} exceeded the "
                        f"{self._max_response_bytes}-byte response limit."
                    )
                chunks.append(chunk)
            return GoogleApiResponse(
                status_code=response.status_code, content=b"".join(chunks)
            )
