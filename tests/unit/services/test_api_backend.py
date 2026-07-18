"""Unit tests for :class:`HttpApiBackend`."""

from __future__ import annotations

import httpx
import pytest

from family_assistant.services.api_backend import (
    ApiBackend,
    ApiBackendError,
    HttpApiBackend,
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
    backend = HttpApiBackend(client)

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
    assert isinstance(backend, ApiBackend)


@pytest.mark.asyncio
async def test_json_helper_decodes_body() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "1"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    backend = HttpApiBackend(client)

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
    backend = HttpApiBackend(client)

    response = await backend.request(
        method="GET",
        url="https://gmail.googleapis.com/gmail/v1/users/me/messages/abc",
        access_token="tok",
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_response_body_over_cap_raises_google_api_error() -> None:
    body = b"x" * (64 * 1024)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    backend = HttpApiBackend(client, max_response_bytes=1024)

    with pytest.raises(ApiBackendError, match="response limit"):
        await backend.request(
            method="GET",
            url="https://www.googleapis.com/drive/v3/files/huge?alt=media",
            access_token="tok",
        )


@pytest.mark.asyncio
async def test_response_body_within_cap_is_returned() -> None:
    body = b"small-body"

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    backend = HttpApiBackend(client, max_response_bytes=1024)

    response = await backend.request(
        method="GET",
        url="https://www.googleapis.com/drive/v3/files/small?alt=media",
        access_token="tok",
    )

    assert response.status_code == 200
    assert response.content == body


@pytest.mark.asyncio
async def test_transport_failure_raises_google_api_error() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    backend = HttpApiBackend(client)

    with pytest.raises(ApiBackendError):
        await backend.request(
            method="GET",
            url="https://gmail.googleapis.com/gmail/v1/users/me/profile",
            access_token="tok",
        )
