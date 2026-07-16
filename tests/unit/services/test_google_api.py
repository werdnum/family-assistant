"""Unit tests for :class:`HttpGoogleApiBackend`."""

from __future__ import annotations

import httpx
import pytest

from family_assistant.services.google_api import (
    GoogleApiBackend,
    GoogleApiError,
    HttpGoogleApiBackend,
)


@pytest.mark.asyncio
async def test_sets_bearer_header_and_returns_status_and_content() -> None:
    captured: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b"payload-bytes")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    backend = HttpGoogleApiBackend(client)

    response = await backend.request(
        method="GET",
        url="https://gmail.googleapis.com/gmail/v1/users/me/messages",
        access_token="tok-123",
        params={"q": "from:school"},
    )

    assert response.status_code == 200
    assert response.content == b"payload-bytes"
    assert captured["authorization"] == "Bearer tok-123"
    assert captured["method"] == "GET"
    assert "q=from%3Aschool" in str(captured["url"])
    assert isinstance(backend, GoogleApiBackend)


@pytest.mark.asyncio
async def test_json_helper_decodes_body() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "1"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    backend = HttpGoogleApiBackend(client)

    response = await backend.request(
        method="GET",
        url="https://www.googleapis.com/drive/v3/files",
        access_token="tok",
    )

    assert response.json() == {"messages": [{"id": "1"}]}


@pytest.mark.asyncio
async def test_error_status_is_returned_not_raised() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    backend = HttpGoogleApiBackend(client)

    response = await backend.request(
        method="GET",
        url="https://gmail.googleapis.com/gmail/v1/users/me/messages/abc",
        access_token="tok",
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_transport_failure_raises_google_api_error() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    backend = HttpGoogleApiBackend(client)

    with pytest.raises(GoogleApiError):
        await backend.request(
            method="GET",
            url="https://gmail.googleapis.com/gmail/v1/users/me/profile",
            access_token="tok",
        )
