from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from family_assistant.config_models import AppConfig, UCPConfig
from family_assistant.tools import shopping

if TYPE_CHECKING:
    from pytest import MonkeyPatch

    from family_assistant.tools.types import ToolExecutionContext


def _private_key_pem() -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@dataclass
class _RecordedRequest:
    url: str
    headers: dict[str, str]
    body: dict[str, object]


class _FakeAsyncClient:
    requests: list[_RecordedRequest] = []
    response_json: dict[str, object] = {}

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None,
    ) -> httpx.Response:
        body = json.loads((content or b"{}").decode("utf-8"))
        self.requests.append(_RecordedRequest(url=url, headers=headers, body=body))
        return httpx.Response(200, json=self.response_json)


def _context(app_config: AppConfig) -> ToolExecutionContext:
    return cast(
        "ToolExecutionContext",
        SimpleNamespace(processing_service=SimpleNamespace(app_config=app_config)),
    )


def _cart_response() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "result": {
            "structuredContent": {
                "cart": {
                    "id": "gid://shopify/Cart/cart_abc123",
                    "continue_url": "https://shop.example.com/cart/c/cart_abc123",
                    "line_items": [],
                }
            }
        },
    }


def _checkout_response() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "result": {
            "structuredContent": {
                "id": "gid://shopify/Checkout/checkout_abc123",
                "status": "requires_escalation",
                "continue_url": "https://shop.example.com/checkouts/c/checkout_abc123",
            }
        },
    }


async def test_shopify_add_to_cart_creates_unsigned_cart_request(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.response_json = _cart_response()

    result = await shopping.shopify_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[
            {
                "variant_id": "gid://shopify/ProductVariant/12345678901",
                "quantity": 2,
            }
        ],
        context={"address_country": "US"},
    )

    assert "https://shop.example.com/cart/c/cart_abc123" in result.get_text()
    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop.example.com/api/ucp/mcp"
    assert request.headers["UCP-Agent"] == (
        'profile="https://assistant.example/.well-known/ucp"'
    )
    assert "Signature" not in request.headers
    assert request.body["method"] == "tools/call"
    params = cast("dict[str, object]", request.body["params"])
    assert params["name"] == "create_cart"
    arguments = cast("dict[str, object]", params["arguments"])
    assert cast("dict[str, object]", arguments["meta"])["ucp-agent"] == {
        "profile": "https://assistant.example/.well-known/ucp"
    }


async def test_shopify_transfer_checkout_to_human_returns_signed_continue_url(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.response_json = _checkout_response()
    app_config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    result = await shopping.shopify_transfer_checkout_to_human_tool(
        _context(app_config),
        business_url="https://shop.example.com",
        cart_id="gid://shopify/Cart/cart_abc123",
    )

    assert "https://shop.example.com/checkouts/c/checkout_abc123" in result.get_text()
    assert "buyer must complete payment" in result.get_text()
    request = _FakeAsyncClient.requests[0]
    assert "Signature" in request.headers
    params = cast("dict[str, object]", request.body["params"])
    assert params["name"] == "create_checkout"
    arguments = cast("dict[str, object]", params["arguments"])
    assert arguments["cart_id"] == "gid://shopify/Cart/cart_abc123"
