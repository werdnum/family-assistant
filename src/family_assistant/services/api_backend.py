"""Provider-neutral bearer-token REST seam used by connected-account tools.

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

# Default per-request timeout for REST calls (seconds).
_DEFAULT_TIMEOUT_SECONDS = 30.0

# Hard ceiling on a response body read from a REST endpoint (bytes). The
# body is streamed and aborted once this budget is exceeded, so a multi-gigabyte
# Drive download (``alt=media``) or a hostile export can never be materialized in
# memory. 25 MiB matches the Drive download pre-check in ``tools/google_data.py``.
_DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024


class ApiBackendError(Exception):
    """Raised on a transport-level failure talking to a REST endpoint.

    HTTP error *statuses* are returned to the caller in
    :class:`ApiResponse` (the tools interpret them); this is reserved for
    failures where no response was received (network error, timeout).
    """


@dataclass(frozen=True)
class ApiResponse:
    """A minimal REST response: status code and raw body bytes."""

    status_code: int
    content: bytes

    def json(self) -> Any:  # noqa: ANN401 - arbitrary decoded JSON shape
        """Decode the body as JSON."""
        return json.loads(self.content)


@runtime_checkable
class ApiBackend(Protocol):
    """Protocol for the REST backend used by connected-account data tools.

    Implementations MUST enforce a hard response-body cap while reading, raising
    :class:`ApiBackendError` when a body exceeds it, so an unbounded download
    (e.g. Drive ``alt=media`` on a huge file) can never exhaust server memory.
    """

    async def request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        """Issue an authenticated request to a full REST URL."""
        ...


class HttpApiBackend:
    """A :class:`ApiBackend` over an injected ``httpx.AsyncClient``."""

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
    ) -> ApiResponse:
        """Issue an authenticated request, returning status and raw bytes.

        The access token is sent as a Bearer ``Authorization`` header and is
        never logged. The response body is streamed and read against a hard
        ``max_response_bytes`` budget so an unbounded download can never be
        materialized in memory; exceeding it raises :class:`ApiBackendError`.
        Transport-level failures also raise :class:`ApiBackendError`; HTTP error
        statuses are returned for the caller to interpret.
        """
        try:
            return await self._stream_request(
                method=method, url=url, access_token=access_token, params=params
            )
        except httpx.HTTPError as exc:
            raise ApiBackendError(f"API request to {url} failed") from exc

    async def _stream_request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None,
    ) -> ApiResponse:
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
                    raise ApiBackendError(
                        f"API response from {url} exceeded the "
                        f"{self._max_response_bytes}-byte response limit."
                    )
                chunks.append(chunk)
            return ApiResponse(
                status_code=response.status_code, content=b"".join(chunks)
            )
