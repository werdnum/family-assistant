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
    """Protocol for the Google REST backend used by the Gmail/Drive tools."""

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
    ) -> None:
        """Initialize with an async HTTP client and per-request timeout."""
        self._http_client = http_client
        self._timeout = timeout

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
        never logged. Transport-level failures raise :class:`GoogleApiError`;
        HTTP error statuses are returned for the caller to interpret.
        """
        try:
            response = await self._http_client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params=dict(params) if params is not None else None,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GoogleApiError(f"Google API request to {url} failed") from exc
        return GoogleApiResponse(
            status_code=response.status_code, content=response.content
        )
