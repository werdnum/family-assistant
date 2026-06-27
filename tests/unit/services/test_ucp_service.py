from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from family_assistant.config_models import AppConfig, UCPConfig
from family_assistant.services.ucp import (
    MerchantUCPProfile,
    UCPConfigurationError,
    build_ucp_profile,
    discover_merchant_ucp_profile,
    host_matches_trusted_suffix,
    merchant_origin,
    same_site,
    sign_ucp_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _merchant_profile_payload(
    *, endpoint: str | None = "https://shop.example.com/api/ucp/mcp"
) -> dict[str, object]:
    binding: dict[str, object] = {"transport": "mcp"}
    if endpoint is not None:
        binding["endpoint"] = endpoint
    return {
        "ucp": {
            "version": "2026-04-08",
            "services": {"dev.ucp.shopping": [binding]},
            "capabilities": {
                "dev.ucp.shopping.cart": [{}],
                "dev.ucp.shopping.checkout": [{}],
            },
        }
    }


def _client_returning(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _private_key_pem() -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def test_build_ucp_profile_publishes_public_jwk_for_signing_key() -> None:
    config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    profile = cast("dict[str, Any]", build_ucp_profile(config))

    assert profile["ucp"]["version"] == "2026-04-08"
    assert "dev.ucp.shopping" in profile["ucp"]["services"]
    assert profile["ucp"]["payment_handlers"] == {}
    shopping_service = profile["ucp"]["services"]["dev.ucp.shopping"][0]
    assert shopping_service["transport"] == "mcp"
    assert shopping_service["schema"].endswith("/shopping/mcp.openrpc.json")
    assert "dev.ucp.shopping.cart" in profile["ucp"]["capabilities"]
    assert "dev.ucp.shopping.checkout" in profile["ucp"]["capabilities"]
    assert profile["signing_keys"][0]["kid"] == "platform-2026"
    assert profile["signing_keys"][0]["crv"] == "P-256"
    assert profile["signing_keys"][0]["alg"] == "ES256"
    assert "d" not in profile["signing_keys"][0]


def test_sign_ucp_request_adds_required_headers_for_json_post() -> None:
    config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    request = sign_ucp_request(
        config,
        method="POST",
        url="https://merchant.example/ucp/v1/checkout-sessions?debug=true",
        body={"checkout": {"line_items": [{"id": "sku_123", "quantity": 1}]}},
        idempotency_key="00000000-0000-4000-8000-000000000000",
    )

    assert request.method == "POST"
    assert request.body == (
        b'{"checkout":{"line_items":[{"id":"sku_123","quantity":1}]}}'
    )
    assert request.headers["UCP-Agent"] == (
        'profile="https://assistant.example/.well-known/ucp"'
    )
    assert request.headers["Idempotency-Key"] == (
        "00000000-0000-4000-8000-000000000000"
    )
    assert request.headers["Content-Digest"].startswith("sha-256=:")
    assert '"@query"' in request.headers["Signature-Input"]
    assert '"content-digest"' in request.headers["Signature-Input"]
    signature_value = (
        request.headers["Signature"].removeprefix("sig1=:").removesuffix(":")
    )
    assert len(base64.b64decode(signature_value)) == 64


def test_sign_ucp_get_request_omits_body_headers_and_idempotency_key() -> None:
    config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    request = sign_ucp_request(
        config,
        method="GET",
        url="https://merchant.example/ucp/v1/products/sku_123",
    )

    assert "Content-Digest" not in request.headers
    assert "Content-Type" not in request.headers
    assert "Idempotency-Key" not in request.headers
    assert request.body is None


def test_sign_ucp_request_rejects_non_https_profile_url() -> None:
    config = AppConfig(
        server_url="http://localhost:8000",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    with pytest.raises(UCPConfigurationError, match="profile URL must be an HTTPS"):
        sign_ucp_request(
            config,
            method="POST",
            url="https://merchant.example/api/ucp/mcp",
            body={"jsonrpc": "2.0"},
        )


def test_ucp_config_rejects_custom_profile_path_without_profile_url() -> None:
    with pytest.raises(ValidationError, match="profile_url is required"):
        UCPConfig(profile_path="/custom/ucp")


def test_ucp_config_allows_custom_profile_path_with_profile_url() -> None:
    config = UCPConfig(
        profile_path="/custom/ucp",
        profile_url="https://assistant.example/custom/ucp",
    )

    assert config.profile_path == "/custom/ucp"


async def test_discover_merchant_ucp_profile_returns_advertised_endpoint() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=_merchant_profile_payload())

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com/products/sweater", client=client
        )

    assert requested == ["https://shop.example.com/.well-known/ucp"]
    assert profile is not None
    assert profile.origin == "https://shop.example.com"
    assert profile.mcp_endpoint == "https://shop.example.com/api/ucp/mcp"
    assert profile.supports_shopping is True
    assert "dev.ucp.shopping.cart" in profile.capability_names
    assert profile.version == "2026-04-08"


async def test_discover_merchant_ucp_profile_follows_redirect() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/.well-known/ucp":
            return httpx.Response(
                301,
                headers={"Location": "https://shop.example.com/ucp-config"},
            )
        return httpx.Response(200, json=_merchant_profile_payload())

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com/products/sweater", client=client
        )

    # The 301 is followed to the canonical config URL instead of being treated
    # as an empty/undecodable body.
    assert requested == [
        "https://shop.example.com/.well-known/ucp",
        "https://shop.example.com/ucp-config",
    ]
    assert profile is not None
    assert profile.mcp_endpoint == "https://shop.example.com/api/ucp/mcp"


async def test_discovery_redirect_chain_shares_one_timeout_budget() -> None:
    timeouts: list[float | None] = []
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    class _SlowRedirectClient:
        async def get(
            self,
            _url: str,
            *,
            headers: dict[str, str] | None = None,
            timeout: float | None = None,
        ) -> httpx.Response:
            timeouts.append(timeout)
            clock["t"] += 2.0  # each hop "takes" 2s of the budget
            return httpx.Response(
                301, headers={"Location": "https://shop.example.com/next"}
            )

    # Driven through the public API with an injected clock (the `now` seam) so we
    # never reach into the private redirect helper.
    profile = await discover_merchant_ucp_profile(
        "https://shop.example.com",
        client=cast("httpx.AsyncClient", _SlowRedirectClient()),
        timeout=5.0,
        now=now,
    )

    # The 5s budget is shared across hops (5 → 3 → 1), not reapplied per hop, so
    # the chain gives up once the budget is spent instead of running all six
    # allowed redirects at 5s each.
    assert profile is None
    assert timeouts == [5.0, 3.0, 1.0]


async def test_discover_merchant_ucp_profile_handles_malformed_redirect() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # A merchant-controlled Location that urljoin cannot parse must not crash
        # discovery; it is treated as a miss.
        return httpx.Response(302, headers={"Location": "https://[zzz]/ucp"})

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com", client=client
        )

    assert profile is None


async def test_discover_merchant_ucp_profile_ignores_cross_origin_redirect() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "shop.example.com":
            # An off-origin redirect must not be followed (SSRF guard).
            return httpx.Response(
                302,
                headers={"Location": "https://attacker.example/.well-known/ucp"},
            )
        # Excluded from coverage: the off-origin redirect above must never be
        # followed, so this branch is unreachable; if discovery ever fetched the
        # cross-origin target, the assertion fails the test rather than silently
        # passing.
        raise AssertionError(  # pragma: no cover
            "cross-origin redirect target must not be fetched"
        )

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com", client=client
        )

    assert requested == ["https://shop.example.com/.well-known/ucp"]
    assert profile is None


async def test_discover_merchant_ucp_profile_collects_all_endpoints() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ucp": {
                    "services": {
                        "dev.ucp.shopping": [
                            {"transport": "mcp", "endpoint": "https://a.example/mcp"},
                            {"transport": "rest", "endpoint": "https://a.example/rest"},
                            {"transport": "mcp", "endpoint": "https://b.example/mcp"},
                        ]
                    }
                }
            },
        )

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://a.example", client=client
        )

    assert profile is not None
    # Only MCP bindings, in profile order; the first is exposed as mcp_endpoint.
    assert profile.mcp_endpoints == (
        "https://a.example/mcp",
        "https://b.example/mcp",
    )
    assert profile.mcp_endpoint == "https://a.example/mcp"


def test_merchant_origin_returns_none_for_malformed_url() -> None:
    # urlparse raises ValueError on this host; merchant_origin must swallow it.
    assert merchant_origin("https://[zzz]/mcp") is None


async def test_discover_merchant_ucp_profile_skips_malformed_endpoint() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ucp": {
                    "services": {
                        "dev.ucp.shopping": [
                            {"transport": "mcp", "endpoint": "https://[zzz]/mcp"},
                            {
                                "transport": "mcp",
                                "endpoint": "https://shop.example.com/mcp",
                            },
                        ]
                    }
                }
            },
        )

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com", client=client
        )

    # The malformed binding is skipped, not allowed to break discovery.
    assert profile is not None
    assert profile.mcp_endpoints == ("https://shop.example.com/mcp",)


async def test_discover_merchant_ucp_profile_returns_none_on_undecodable_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\xff\xfe\xfa",
            headers={"Content-Type": "application/json"},
        )

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com", client=client
        )

    assert profile is None


def test_usable_mcp_endpoint_prefers_same_origin_binding() -> None:
    profile = MerchantUCPProfile(
        origin="https://shop.example.com",
        mcp_endpoints=(
            "https://other.unrelated.com/mcp",
            "https://shop.example.com:443/ucp/rpc",
        ),
        service_names=("dev.ucp.shopping",),
        capability_names=(),
        version=None,
    )

    # Skips the untrusted cross-host binding; matches the same-origin one.
    assert profile.usable_mcp_endpoint() == "https://shop.example.com:443/ucp/rpc"


def test_usable_mcp_endpoint_none_when_only_untrusted_cross_host() -> None:
    profile = MerchantUCPProfile(
        origin="https://shop.example.com",
        mcp_endpoints=("https://other.unrelated.com/mcp",),
        service_names=("dev.ucp.shopping",),
        capability_names=(),
        version=None,
    )

    assert profile.usable_mcp_endpoint() is None
    assert profile.supports_shopping is True


def test_usable_mcp_endpoint_accepts_same_site_subdomain() -> None:
    # THE ICONIC serves its endpoint from a sibling subdomain of the storefront.
    profile = MerchantUCPProfile(
        origin="https://www.theiconic.com.au",
        mcp_endpoints=("https://eve.theiconic.com.au/ucp/v1",),
        service_names=("dev.ucp.shopping",),
        capability_names=(),
        version=None,
    )

    assert profile.usable_mcp_endpoint() == "https://eve.theiconic.com.au/ucp/v1"


def test_usable_mcp_endpoint_accepts_trusted_suffix_only_when_configured() -> None:
    # A Shopify storefront on a custom domain advertises its *.myshopify.com host.
    profile = MerchantUCPProfile(
        origin="https://statusanxiety.com.au",
        mcp_endpoints=("https://status-anxiety-2.myshopify.com/api/ucp/mcp",),
        service_names=("dev.ucp.shopping",),
        capability_names=(),
        version=None,
    )

    assert profile.usable_mcp_endpoint() is None
    assert profile.usable_mcp_endpoint(trusted_suffixes=("myshopify.com",)) == (
        "https://status-anxiety-2.myshopify.com/api/ucp/mcp"
    )


def test_same_site_uses_registrable_domain() -> None:
    assert same_site("https://eve.theiconic.com.au/x", "https://www.theiconic.com.au")
    # Different registrable domains under the same multi-label public suffix.
    assert not same_site("https://statusanxiety.com.au", "https://example.com.au")
    assert not same_site("http://eve.theiconic.com.au", "https://www.theiconic.com.au")


def test_host_matches_trusted_suffix_is_label_anchored() -> None:
    suffixes = ("myshopify.com",)
    assert host_matches_trusted_suffix(
        "https://status-anxiety-2.myshopify.com/api/ucp/mcp", suffixes
    )
    assert host_matches_trusted_suffix("https://myshopify.com/api", suffixes)
    assert not host_matches_trusted_suffix("https://notmyshopify.com/api", suffixes)
    assert not host_matches_trusted_suffix(
        "https://myshopify.com.evil.com/api", suffixes
    )
    assert not host_matches_trusted_suffix("http://shop.myshopify.com/api", suffixes)


def test_usable_mcp_endpoint_prefers_same_origin_over_broader_trust() -> None:
    # A same-origin binding listed after a trusted-suffix and a same-site one is
    # still chosen — the safest merchant-local endpoint wins over platform/
    # same-site fallbacks regardless of profile order.
    profile = MerchantUCPProfile(
        origin="https://shop.example.com",
        mcp_endpoints=(
            "https://shop-2.myshopify.com/api/ucp/mcp",
            "https://other.example.com/mcp",
            "https://shop.example.com/ucp/rpc",
        ),
        service_names=("dev.ucp.shopping",),
        capability_names=(),
        version=None,
    )

    assert profile.usable_mcp_endpoint(trusted_suffixes=("myshopify.com",)) == (
        "https://shop.example.com/ucp/rpc"
    )


def test_same_site_rejects_ip_and_internal_hosts() -> None:
    # get_sld would collapse these unrelated hosts to a shared token; strict
    # resolution rejects hosts without a real public suffix so they never match.
    assert not same_site("https://203.0.113.1/mcp", "https://10.0.0.1")
    assert not same_site("https://foo.internal/mcp", "https://bar.internal")
    assert not same_site("https://10.0.0.1/mcp", "https://10.0.0.1")


def test_same_site_separates_multi_tenant_platform_hosts() -> None:
    # The vendored current PSL lists modern multi-tenant suffixes in its PRIVATE
    # section, so co-tenants are distinct registrable domains and never same-site
    # (a stale 2019 list would collapse both to the platform domain).
    assert not same_site("https://foo.vercel.app/mcp", "https://bar.vercel.app")
    assert not same_site("https://foo.pages.dev/mcp", "https://bar.pages.dev")
    # A merchant's own sibling subdomain is still same-site.
    assert same_site("https://eve.theiconic.com.au/x", "https://www.theiconic.com.au")


def test_https_trust_rejects_malformed_port() -> None:
    # An out-of-range port must be treated as a discovery miss, not silently
    # matched on the bare host.
    assert not same_site("https://shop.example.com:99999/x", "https://shop.example.com")
    assert not host_matches_trusted_suffix(
        "https://shop.myshopify.com:99999/x", ("myshopify.com",)
    )


def test_https_trust_requires_default_port() -> None:
    # Same-site / trusted-suffix matching trusts only the standard HTTPS service;
    # an alternate port could reach a different service on the same host, so it
    # is rejected, while an explicit :443 is accepted as the default.
    assert not same_site(
        "https://eve.theiconic.com.au:8443/x", "https://www.theiconic.com.au"
    )
    assert same_site(
        "https://eve.theiconic.com.au:443/x", "https://www.theiconic.com.au"
    )
    assert not host_matches_trusted_suffix(
        "https://shop.myshopify.com:8443/x", ("myshopify.com",)
    )
    assert host_matches_trusted_suffix(
        "https://shop.myshopify.com:443/x", ("myshopify.com",)
    )


async def test_discover_merchant_ucp_profile_resolves_relative_endpoint() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_merchant_profile_payload(endpoint="/ucp/mcp"))

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com", client=client
        )

    assert profile is not None
    assert profile.mcp_endpoint == "https://shop.example.com/ucp/mcp"


async def test_discover_merchant_ucp_profile_ignores_endpoint_without_host() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_merchant_profile_payload(endpoint="https:/ucp/mcp")
        )

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com", client=client
        )

    assert profile is not None
    assert profile.mcp_endpoint is None


async def test_discover_merchant_ucp_profile_without_shopping_binding() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ucp": {"services": {}}})

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com", client=client
        )

    assert profile is not None
    assert profile.mcp_endpoint is None
    assert profile.supports_shopping is False


async def test_discover_merchant_ucp_profile_returns_none_on_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "https://shop.example.com", client=client
        )

    assert profile is None


async def test_discover_merchant_ucp_profile_rejects_non_https_origin() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # Excluded from coverage: discovery returns before issuing any HTTP
        # request for a non-HTTPS origin, so this guard must never run. If it
        # does, the assertion below fails the test rather than silently passing.
        raise AssertionError(  # pragma: no cover
            "non-HTTPS origin must not be fetched"
        )

    async with _client_returning(handler) as client:
        profile = await discover_merchant_ucp_profile(
            "http://shop.example.com", client=client
        )

    assert profile is None
