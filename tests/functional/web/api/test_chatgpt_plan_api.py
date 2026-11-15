"""The ChatGPT plan sign-in endpoints, driven over HTTP.

The router runs on a bare FastAPI app with the real ``ChatGPTPlanAuth`` and
repository; OpenAI's token endpoint is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from family_assistant.services.chatgpt_plan import (
    REDIRECT_URI,
    SCOPES,
    ChatGPTPlanAuth,
)
from family_assistant.services.credential_encryption import (
    CredentialEncryption,
    generate_key,
)
from family_assistant.storage.database import Database
from family_assistant.web.dependencies import get_current_session_user
from family_assistant.web.routers.chatgpt_plan_api import chatgpt_plan_api_router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

API = "/api/integrations/chatgpt"


def _id_token(nonce: str) -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    claims = {
        "iss": "https://auth.openai.com",
        "aud": "oaiapp_issued",
        "sub": "subject-1",
        "email": "parent@example.com",
        "nonce": nonce,
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    }
    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


class _TokenEndpoint:
    def __init__(self) -> None:
        self.nonce = ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": _id_token(self.nonce),
                "expires_in": 3600,
                "scope": SCOPES,
            },
        )


def _app(auth: ChatGPTPlanAuth | None) -> FastAPI:
    app = FastAPI()
    app.include_router(chatgpt_plan_api_router, prefix=API)
    app.state.chatgpt_plan = auth
    app.dependency_overrides[get_current_session_user] = lambda: {
        "user_identifier": "alice"
    }
    return app


@pytest.fixture
def token_endpoint() -> _TokenEndpoint:
    return _TokenEndpoint()


@pytest_asyncio.fixture
async def client(
    db_engine: AsyncEngine, token_endpoint: _TokenEndpoint
) -> AsyncGenerator[AsyncClient]:
    auth = ChatGPTPlanAuth(
        Database(engine=db_engine),
        CredentialEncryption(generate_key()),
        httpx.AsyncClient(transport=httpx.MockTransport(token_endpoint)),
        agent_name="Family Assistant",
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(auth)), base_url="http://test"
    ) as http:
        yield http


async def test_status_before_any_sign_in(client: AsyncClient) -> None:
    response = await client.get(API)

    assert response.json()["enabled"] is True
    assert response.json()["status"] is None
    assert response.json()["redirect_uri"] == REDIRECT_URI


async def test_pasting_the_callback_signs_the_deployment_in(
    client: AsyncClient, token_endpoint: _TokenEndpoint
) -> None:
    started = await client.post(f"{API}/sign-in", json={"use_saved_account": True})
    authorize = {
        key: values[0]
        for key, values in parse_qs(
            urlsplit(started.json()["authorize_url"]).query
        ).items()
    }
    token_endpoint.nonce = authorize["nonce"]
    callback = f"{REDIRECT_URI}?" + urlencode({
        "code": "c",
        "state": authorize["state"],
        "client_id": "oaiapp_issued",
    })

    response = await client.post(f"{API}/callback", json={"callback_url": callback})

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["email"] == "parent@example.com"
    assert response.json()["connected_by_user_id"] == "alice"


async def test_unusable_callback_is_a_bad_request(client: AsyncClient) -> None:
    await client.post(f"{API}/sign-in", json={})

    response = await client.post(
        f"{API}/callback", json={"callback_url": f"{REDIRECT_URI}?state=wrong"}
    )

    assert response.status_code == 400
    assert "current sign-in attempt" in response.json()["detail"]


async def test_without_an_encryption_key_sign_in_is_unavailable() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(None)), base_url="http://test"
    ) as http:
        status = await http.get(API)
        started = await http.post(f"{API}/sign-in", json={})

    assert status.json()["enabled"] is False
    assert started.status_code == 409
