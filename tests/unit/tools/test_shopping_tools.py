from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest
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
    responses: list[httpx.Response] = []
    # Profile responses returned by GET /.well-known/ucp during discovery.
    # Defaults to a 404 so tools fall back to the Shopify endpoint convention.
    profile_responses: list[httpx.Response] = []
    profile_requests: list[str] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        self.profile_requests.append(url)
        if self.profile_responses:
            return self.profile_responses.pop(0)
        return httpx.Response(404)

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None,
    ) -> httpx.Response:
        body = json.loads((content or b"{}").decode("utf-8"))
        self.requests.append(_RecordedRequest(url=url, headers=headers, body=body))
        if not self.responses:
            msg = "No fake UCP response configured."
            raise AssertionError(msg)
        return self.responses.pop(0)


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
                "id": "gid://shopify/Cart/cart_abc123",
                "continue_url": "https://shop.example.com/cart/c/cart_abc123",
                "line_items": [],
            }
        },
    }


def _checkout_only_profile() -> dict[str, object]:
    # Advertises the checkout capability but not cart: a checkout-only merchant.
    return {
        "ucp": {
            "services": {
                "dev.ucp.shopping": [
                    {
                        "transport": "mcp",
                        "endpoint": "https://shop.example.com/ucp/rpc",
                    }
                ]
            },
            "capabilities": {"dev.ucp.shopping.checkout": [{}]},
        }
    }


def _cart_and_checkout_profile() -> dict[str, object]:
    # Advertises both cart and checkout: the normal cart flow applies.
    return {
        "ucp": {
            "services": {
                "dev.ucp.shopping": [
                    {
                        "transport": "mcp",
                        "endpoint": "https://shop.example.com/ucp/rpc",
                    }
                ]
            },
            "capabilities": {
                "dev.ucp.shopping.cart": [{}],
                "dev.ucp.shopping.checkout": [{}],
            },
        }
    }


def _cart_with_items_response() -> dict[str, object]:
    # A get_cart result with line items, used by the checkout handoff which now
    # reads the cart and builds the checkout from its contents.
    return {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "result": {
            "structuredContent": {
                "id": "gid://shopify/Cart/cart_abc123",
                "line_items": [
                    {
                        "quantity": 1,
                        "item": {"id": "gid://shopify/ProductVariant/12345678901"},
                    }
                ],
                "continue_url": "https://shop.example.com/cart/c/cart_abc123",
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


async def test_ucp_add_to_cart_creates_unsigned_cart_request(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_responses = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.responses = [httpx.Response(200, json=_cart_response())]

    result = await shopping.ucp_add_to_cart_tool(
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
    # Discovery probes the merchant profile; with no profile served (404) it
    # retries the .json fallback, then falls back to the Shopify convention.
    assert _FakeAsyncClient.profile_requests == [
        "https://shop.example.com/.well-known/ucp",
        "https://shop.example.com/.well-known/ucp.json",
    ]
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


async def test_ucp_add_to_cart_uses_discovered_merchant_endpoint(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = [
        httpx.Response(
            200,
            json={
                "ucp": {
                    "services": {
                        "dev.ucp.shopping": [
                            {
                                "transport": "mcp",
                                "endpoint": "https://shop.example.com/ucp/rpc",
                            }
                        ]
                    }
                }
            },
        )
    ]
    _FakeAsyncClient.responses = [httpx.Response(200, json=_cart_response())]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "variant-1", "quantity": 1}],
    )

    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop.example.com/ucp/rpc"


async def test_ucp_add_to_cart_uses_trusted_platform_endpoint_cross_site(
    monkeypatch: MonkeyPatch,
) -> None:
    # A Shopify storefront on a custom domain advertises its *.myshopify.com shop
    # host; the default trusted suffix lets that cross-site endpoint be used
    # instead of falling back to a non-existent custom-domain path.
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = [
        httpx.Response(
            200,
            json={
                "ucp": {
                    "services": {
                        "dev.ucp.shopping": [
                            {
                                "transport": "mcp",
                                "endpoint": "https://shop-2.myshopify.com/api/ucp/mcp",
                            }
                        ]
                    }
                }
            },
        )
    ]
    _FakeAsyncClient.responses = [httpx.Response(200, json=_cart_response())]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://statusanxiety.example/products/wallet",
        line_items=[{"variant_id": "variant-1", "quantity": 1}],
    )

    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop-2.myshopify.com/api/ucp/mcp"


async def test_ucp_add_to_cart_rejects_cross_origin_discovered_endpoint(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = [
        httpx.Response(
            200,
            json={
                "ucp": {
                    "services": {
                        "dev.ucp.shopping": [
                            {
                                "transport": "mcp",
                                "endpoint": "https://internal-host/api/ucp/mcp",
                            }
                        ]
                    }
                }
            },
        )
    ]
    _FakeAsyncClient.responses = [httpx.Response(200, json=_cart_response())]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "variant-1", "quantity": 1}],
    )

    # A cross-origin endpoint is ignored; the POST stays on the merchant origin.
    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop.example.com/api/ucp/mcp"


async def test_ucp_add_to_cart_prefers_same_origin_over_cross_origin_binding(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = [
        httpx.Response(
            200,
            json={
                "ucp": {
                    "services": {
                        "dev.ucp.shopping": [
                            {
                                "transport": "mcp",
                                "endpoint": "https://internal-host/api/ucp/mcp",
                            },
                            {
                                "transport": "mcp",
                                "endpoint": "https://shop.example.com/ucp/rpc",
                            },
                        ]
                    }
                }
            },
        )
    ]
    _FakeAsyncClient.responses = [httpx.Response(200, json=_cart_response())]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "variant-1", "quantity": 1}],
    )

    # The same-origin binding is used even though a cross-origin one is listed
    # first.
    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop.example.com/ucp/rpc"


async def test_ucp_add_to_cart_accepts_same_origin_with_default_port(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = [
        httpx.Response(
            200,
            json={
                "ucp": {
                    "services": {
                        "dev.ucp.shopping": [
                            {
                                "transport": "mcp",
                                "endpoint": "https://shop.example.com:443/ucp/rpc",
                            }
                        ]
                    }
                }
            },
        )
    ]
    _FakeAsyncClient.responses = [httpx.Response(200, json=_cart_response())]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "variant-1", "quantity": 1}],
    )

    # An explicit :443 is the same origin as the default-port business_url.
    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop.example.com:443/ucp/rpc"


async def test_ucp_add_to_existing_cart_resolves_endpoint_once(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = []  # 404 -> Shopify fallback
    _FakeAsyncClient.responses = [
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "rpc-1",
                "result": {
                    "structuredContent": {
                        "id": "gid://shopify/Cart/cart_abc123",
                        "line_items": [],
                        "continue_url": "https://shop.example.com/cart/c/abc",
                    }
                },
            },
        ),
        httpx.Response(200, json=_cart_response()),
    ]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com",
        cart_id="gid://shopify/Cart/cart_abc123",
        line_items=[{"variant_id": "variant-1", "quantity": 1}],
    )

    # get_cart + update_cart share a single endpoint resolution; the unserved
    # profile (404) probes the well-known path then its .json fallback once.
    assert _FakeAsyncClient.profile_requests == [
        "https://shop.example.com/.well-known/ucp",
        "https://shop.example.com/.well-known/ucp.json",
    ]
    assert len(_FakeAsyncClient.requests) == 2


async def test_ucp_transfer_checkout_to_human_returns_signed_continue_url(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.responses = [
        httpx.Response(200, json=_cart_with_items_response()),
        httpx.Response(200, json=_checkout_response()),
    ]
    app_config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    result = await shopping.ucp_transfer_checkout_to_human_tool(
        _context(app_config),
        business_url="https://shop.example.com",
        cart_id="gid://shopify/Cart/cart_abc123",
    )

    assert "https://shop.example.com/checkouts/c/checkout_abc123" in result.get_text()
    assert "buyer must complete payment" in result.get_text()
    # The handoff reads the cart, then creates a checkout from its line items.
    get_cart_request = _FakeAsyncClient.requests[0]
    assert cast("dict[str, object]", get_cart_request.body["params"])["name"] == (
        "get_cart"
    )
    request = _FakeAsyncClient.requests[1]
    assert "Signature" in request.headers
    params = cast("dict[str, object]", request.body["params"])
    assert params["name"] == "create_checkout"
    arguments = cast("dict[str, object]", params["arguments"])
    # cart_id rides inside the checkout object (cart capability extension), not
    # at the arguments root, and converts the existing cart.
    assert "cart_id" not in arguments
    checkout_payload = cast("dict[str, object]", arguments["checkout"])
    assert checkout_payload["cart_id"] == "gid://shopify/Cart/cart_abc123"
    assert checkout_payload["line_items"] == [
        {"quantity": 1, "item": {"id": "gid://shopify/ProductVariant/12345678901"}}
    ]


async def test_ucp_transfer_checkout_to_human_requires_signing_before_discovery(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = []

    with pytest.raises(ValueError, match="signing configuration"):
        await shopping.ucp_transfer_checkout_to_human_tool(
            _context(AppConfig(server_url="https://assistant.example")),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/cart_abc123",
        )

    # Fails fast on local config: no merchant discovery or POST is attempted.
    assert _FakeAsyncClient.profile_requests == []
    assert _FakeAsyncClient.requests == []


async def test_ucp_transfer_checkout_to_human_rejects_malformed_signing_key(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = []
    app_config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key="-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----",
        ),
    )

    with pytest.raises(ValueError, match="private key"):
        await shopping.ucp_transfer_checkout_to_human_tool(
            _context(app_config),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/cart_abc123",
        )

    # A present-but-malformed key fails before contacting the merchant.
    assert _FakeAsyncClient.profile_requests == []
    assert _FakeAsyncClient.requests == []


async def test_ucp_transfer_checkout_to_human_rejects_cart_error_outcome(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.responses = [
        httpx.Response(200, json=_cart_with_items_response()),
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "rpc-1",
                "result": {
                    "structuredContent": {
                        "messages": [
                            {
                                "type": "error",
                                "code": "cart_not_found",
                                "content": "Cart was not found or has expired",
                            }
                        ],
                        "continue_url": "https://shop.example.com/checkouts/fallback",
                    }
                },
            },
        ),
    ]
    app_config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    with pytest.raises(ValueError, match="Cart was not found or has expired"):
        await shopping.ucp_transfer_checkout_to_human_tool(
            _context(app_config),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/missing",
        )


async def test_ucp_transfer_checkout_to_human_requires_checkout_id(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.responses = [
        httpx.Response(200, json=_cart_with_items_response()),
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "rpc-1",
                "result": {
                    "structuredContent": {
                        "status": "requires_escalation",
                        "continue_url": "https://shop.example.com/checkouts/fallback",
                    }
                },
            },
        ),
    ]
    app_config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    with pytest.raises(ValueError, match="checkout ID"):
        await shopping.ucp_transfer_checkout_to_human_tool(
            _context(app_config),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/cart_abc123",
        )


async def test_ucp_tool_raises_for_json_rpc_error(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.responses = [
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "rpc-1",
                "error": {
                    "code": -32000,
                    "message": "Unauthorized",
                    "data": {
                        "code": "signature_invalid",
                        "content": "Signature verification failed",
                    },
                },
            },
        )
    ]

    with pytest.raises(ValueError, match="Signature verification failed"):
        await shopping.ucp_get_cart_tool(
            _context(AppConfig(server_url="https://assistant.example")),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/cart_abc123",
        )


async def test_ucp_tool_raises_for_http_error_with_json_body(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.responses = [
        httpx.Response(429, json={"jsonrpc": "2.0", "id": "rpc-1", "result": {}})
    ]

    with pytest.raises(ValueError, match="HTTP error 429"):
        await shopping.ucp_get_cart_tool(
            _context(AppConfig(server_url="https://assistant.example")),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/cart_abc123",
        )


async def test_ucp_get_cart_raises_for_unusable_cart_message(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.responses = [
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "rpc-1",
                "result": {
                    "structuredContent": {
                        "messages": [
                            {
                                "type": "error",
                                "code": "cart_not_found",
                                "content": "Cart was not found or has expired",
                            }
                        ]
                    }
                },
            },
        )
    ]

    with pytest.raises(ValueError, match="Cart was not found or has expired"):
        await shopping.ucp_get_cart_tool(
            _context(AppConfig(server_url="https://assistant.example")),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/missing",
        )


async def test_ucp_get_cart_raises_when_cart_envelope_is_missing(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.responses = [
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "rpc-1",
                "result": {"structuredContent": {"unexpected": {}}},
            },
        )
    ]

    with pytest.raises(ValueError, match="did not include a cart"):
        await shopping.ucp_get_cart_tool(
            _context(AppConfig(server_url="https://assistant.example")),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/cart_abc123",
        )


async def test_ucp_add_to_cart_checkout_only_merchant_skips_cart(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = [
        httpx.Response(200, json=_checkout_only_profile())
    ]
    _FakeAsyncClient.responses = [httpx.Response(200, json=_checkout_response())]
    app_config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    result = await shopping.ucp_add_to_cart_tool(
        _context(app_config),
        business_url="https://shop.example.com/products/serum",
        line_items=[{"variant_id": "variant-1", "quantity": 2}],
        context={"address_country": "AU"},
    )

    assert "https://shop.example.com/checkouts/c/checkout_abc123" in result.get_text()
    # A single create_checkout call from the line items — no cart endpoint hit.
    assert len(_FakeAsyncClient.requests) == 1
    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop.example.com/ucp/rpc"
    assert "Signature" in request.headers
    params = cast("dict[str, object]", request.body["params"])
    assert params["name"] == "create_checkout"
    arguments = cast("dict[str, object]", params["arguments"])
    # Line items are nested under a top-level `checkout` object per the UCP
    # create_checkout binding, not placed at the arguments root.
    checkout_payload = cast("dict[str, object]", arguments["checkout"])
    assert checkout_payload["line_items"] == [
        {"quantity": 2, "item": {"id": "variant-1"}}
    ]
    assert checkout_payload["context"] == {"address_country": "AU"}


async def test_ucp_add_to_cart_checkout_only_rejects_cart_id(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = [
        httpx.Response(200, json=_checkout_only_profile())
    ]
    _FakeAsyncClient.responses = []
    app_config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    with pytest.raises(ValueError, match="only supports checkout"):
        await shopping.ucp_add_to_cart_tool(
            _context(app_config),
            business_url="https://shop.example.com",
            cart_id="gid://shopify/Cart/cart_abc123",
            line_items=[{"variant_id": "variant-1", "quantity": 1}],
        )

    # A checkout-only merchant cannot update a cart, so nothing is posted.
    assert _FakeAsyncClient.requests == []


async def test_ucp_add_to_cart_checkout_only_cross_origin_falls_back_to_cart(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    # Checkout-only capability, but the only binding is cross-origin (unusable).
    _FakeAsyncClient.profile_responses = [
        httpx.Response(
            200,
            json={
                "ucp": {
                    "services": {
                        "dev.ucp.shopping": [
                            {
                                "transport": "mcp",
                                "endpoint": "https://internal-host/ucp/rpc",
                            }
                        ]
                    },
                    "capabilities": {"dev.ucp.shopping.checkout": [{}]},
                }
            },
        )
    ]
    _FakeAsyncClient.responses = [httpx.Response(200, json=_cart_response())]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "variant-1", "quantity": 1}],
    )

    # The checkout-only capability described the refused cross-origin binding, so
    # the Shopify fallback must NOT inherit it: the cart flow runs as normal.
    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop.example.com/api/ucp/mcp"
    params = cast("dict[str, object]", request.body["params"])
    assert params["name"] == "create_cart"


async def test_ucp_add_to_cart_uses_cart_flow_when_cart_capability_advertised(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.profile_requests = []
    _FakeAsyncClient.profile_responses = [
        httpx.Response(200, json=_cart_and_checkout_profile())
    ]
    _FakeAsyncClient.responses = [httpx.Response(200, json=_cart_response())]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "variant-1", "quantity": 1}],
    )

    # When the merchant advertises a cart capability the normal cart flow runs.
    request = _FakeAsyncClient.requests[0]
    assert request.url == "https://shop.example.com/ucp/rpc"
    params = cast("dict[str, object]", request.body["params"])
    assert params["name"] == "create_cart"


async def test_ucp_add_to_existing_cart_preserves_supported_cart_state(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    existing_cart = {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "result": {
            "structuredContent": {
                "id": "gid://shopify/Cart/cart_abc123",
                "line_items": [
                    {
                        "quantity": 1,
                        "item": {"id": "gid://shopify/ProductVariant/existing"},
                    }
                ],
                "context": {"address_country": "US"},
                "buyer": {"identity_token": "buyer-token"},
                "signals": {"attribute_preferences": ["durable"]},
                "continue_url": "https://shop.example.com/cart/c/cart_abc123",
            }
        },
    }
    updated_cart = _cart_response()
    _FakeAsyncClient.responses = [
        httpx.Response(200, json=existing_cart),
        httpx.Response(200, json=updated_cart),
    ]

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com",
        cart_id="gid://shopify/Cart/cart_abc123",
        line_items=[
            {
                "variant_id": "gid://shopify/ProductVariant/new",
                "quantity": 2,
            }
        ],
    )

    update_request = _FakeAsyncClient.requests[1]
    params = cast("dict[str, object]", update_request.body["params"])
    arguments = cast("dict[str, object]", params["arguments"])
    cart_payload = cast("dict[str, object]", arguments["cart"])
    assert cart_payload["context"] == {"address_country": "US"}
    assert cart_payload["buyer"] == {"identity_token": "buyer-token"}
    assert cart_payload["signals"] == {"attribute_preferences": ["durable"]}
    assert cart_payload["line_items"] == [
        {
            "quantity": 1,
            "item": {"id": "gid://shopify/ProductVariant/existing"},
        },
        {"quantity": 2, "item": {"id": "gid://shopify/ProductVariant/new"}},
    ]
