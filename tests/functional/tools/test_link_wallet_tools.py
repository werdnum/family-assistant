"""Functional tests for Milestone 1 Stripe Link Agent Wallet tools.

Exercises all 5 tools against a real SQLite DB (``db_engine``), real
:class:`OAuthCredentialResolver`, an ``httpx.MockTransport`` fake Link API
server, real browser-scope state, and real ``defaults.yaml`` profile policy
rules (zero ``asyncio.sleep``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from family_assistant.config_loader import load_config
from family_assistant.config_models import LinkIntegrationConfig
from family_assistant.services.credential_encryption import (
    CredentialEncryption,
    generate_key,
)
from family_assistant.services.link_provider import (
    LINK_DEFAULT_OAUTH_CLIENT_ID,
    LINK_PROVIDER,
    LinkScope,
)
from family_assistant.services.oauth_credentials import OAuthCredentialResolver
from family_assistant.storage.database import Database
from family_assistant.tools import link_wallet as link_wallet_mod
from family_assistant.tools.browser_session import (
    browser_scope_key,
    clear_browser_session_review_modes,
    clear_handed_back_scopes,
    get_browser_session_review_mode,
    initialize_browser_session_review_mode,
)
from family_assistant.tools.catalog import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.link_wallet import (
    link_cancel_spend_request_tool,
    link_check_spend_request_tool,
    link_create_spend_request_tool,
    link_list_shipping_addresses_tool,
    link_wallet_status_tool,
    normalize_https_origin,
)
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import PolicyEngine, ToolPolicyDecision
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

USER_ID = "user-alice"
AGENTIC_SCOPE = LinkScope.PAYMENT_METHODS_AGENTIC.value
USERINFO_SCOPE = LinkScope.USERINFO_READ.value
LONG_CONTEXT = (
    "Purchasing two bags of whole-bean espresso coffee from Example Roasters as "
    "requested by Alice for weekly household pantry restock."
)


@dataclass
class _FakeBrowserBackend:
    """Minimal stub implementing the ``current_url`` surface checked by origin binding."""

    current_url: str


@dataclass
class _FakeLinkApiServer:
    """Stubs api.link.com endpoints via httpx.MockTransport."""

    spend_create_response: dict[str, object] = field(default_factory=dict)
    spend_retrieve_responses: list[dict[str, object] | None] = field(
        default_factory=list
    )
    spend_cancel_response: dict[str, object] = field(default_factory=dict)
    recorded_requests: list[tuple[str, str, str, dict[str, object] | None]] = field(
        default_factory=list
    )

    def _handler(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        path = parsed.path
        query = parsed.query
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        self.recorded_requests.append((request.method, path, query, body))

        if request.method == "GET" and path == "/userinfo":
            return httpx.Response(
                200,
                json={
                    "email": "alice@example.com",
                    "name": "Alice Example",
                    "phone": "+61400000000",
                    "address": {
                        "line1": "99 Secret Street",
                        "city": "Melbourne",
                        "postal_code": "3000",
                        "country": "AU",
                    },
                    "default_spend_request_currency": "aud",
                    "agent_wallet_spend_limits": {
                        "per_transaction": {"limit": 50000},
                        "daily": {"limit": 500000, "used": 2500, "remaining": 497500},
                        "thirty_day": {
                            "limit": 2000000,
                            "used": 10000,
                            "remaining": 1990000,
                        },
                    },
                    "agent_wallet_step_up": {
                        "status": "not_required",
                        "action_url": None,
                    },
                },
            )

        if request.method == "GET" and path == "/payment-details":
            return httpx.Response(
                200,
                json={
                    "payment_details": [
                        {
                            "id": "pm_eligible_1",
                            "type": "CARD",
                            "is_default": True,
                            "name": "Visa Everyday",
                            "card_details": {"brand": "visa", "last4": "4242"},
                            "capabilities": {
                                "agentic_payments": {
                                    "eligible": True,
                                    "ineligibility_reasons": [],
                                }
                            },
                        },
                        {
                            "id": "pm_restricted_2",
                            "type": "CARD",
                            "is_default": False,
                            "name": "Amex Corporate",
                            "card_details": {"brand": "amex", "last4": "0005"},
                            "capabilities": {
                                "agentic_payments": {
                                    "eligible": False,
                                    "ineligibility_reasons": ["unsupported_network"],
                                }
                            },
                        },
                        {
                            "id": "pm_no_caps_3",
                            "type": "CARD",
                            "is_default": False,
                            "name": "Mastercard Backup",
                            "card_details": {"brand": "mastercard", "last4": "5555"},
                        },
                    ]
                },
            )

        if request.method == "GET" and path == "/shipping_addresses":
            return httpx.Response(
                200,
                json={
                    "shipping_addresses": [
                        {
                            "id": "addr_home_1",
                            "is_default": True,
                            "nickname": "Home",
                            "address": {
                                "name": "Alice Example",
                                "line_1": "123 Collins St",
                                "locality": "Melbourne",
                                "administrative_area": "VIC",
                                "postal_code": "3000",
                                "country_code": "AU",
                            },
                        }
                    ]
                },
            )

        if request.method == "POST" and path == "/spend_requests":
            return httpx.Response(200, json=self.spend_create_response)

        if request.method == "GET" and path.startswith("/spend_requests/"):
            if self.spend_retrieve_responses:
                nxt = self.spend_retrieve_responses.pop(0)
                if nxt is None:
                    return httpx.Response(404, json={"error": "not_found"})
                return httpx.Response(200, json=nxt)
            return httpx.Response(404, json={"error": "not_found"})

        if request.method == "POST" and path.endswith("/cancel"):
            return httpx.Response(200, json=self.spend_cancel_response)

        return httpx.Response(404, json={"error": "unknown_route"})


def _base_spend_request(
    *,
    sr_id: str = "sr_123",
    status: str = "approved",
    approval_url: str | None = None,
    status_details: dict[str, object] | None = None,
    payment_status_details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": sr_id,
        "merchant_name": "Example Roasters",
        "merchant_url": "https://shop.example.com/checkout",
        "amount": 4500,
        "currency": "aud",
        "status": status,
        "card_brand": "visa",
        "card_last4": "4242",
        "created_at": "2026-11-24T12:00:00Z",
        "updated_at": "2026-11-24T12:00:01Z",
    }
    if approval_url is not None:
        payload["approval_url"] = approval_url
    if status_details is not None:
        payload["status_details"] = status_details
    if payment_status_details is not None:
        payload["payment_status_details"] = payment_status_details
    return payload


@dataclass
class _WalletFixture:
    db: Database
    server: _FakeLinkApiServer
    resolver: OAuthCredentialResolver
    clock: MockClock
    exec_context: ToolExecutionContext


@pytest_asyncio.fixture
async def wallet(db_engine: AsyncEngine) -> AsyncGenerator[_WalletFixture]:
    clear_browser_session_review_modes()
    clear_handed_back_scopes()

    key = generate_key()
    encryption = CredentialEncryption(key)
    clock = MockClock(datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    server = _FakeLinkApiServer()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(server._handler))
    cfg = LinkIntegrationConfig.model_validate({
        "enabled": True,
        "oauth_client_id": LINK_DEFAULT_OAUTH_CLIENT_ID,
        "oauth_client_secret": "",
        "credential_encryption_key": key,
        "scopes": [AGENTIC_SCOPE, USERINFO_SCOPE],
        "require_taint_enforcement": False,
    })
    resolver = OAuthCredentialResolver(
        LINK_PROVIDER, cfg, encryption, http_client, notifier=None, clock=clock
    )

    db = Database(engine=db_engine)
    conn = await db.oauth_connections.upsert_connection(
        user_id=USER_ID,
        provider=LINK_PROVIDER.name,
        provider_account_email="alice@example.com",
        scopes=[AGENTIC_SCOPE, USERINFO_SCOPE],
        refresh_token_encrypted=encryption.encrypt("rt-1"),
    )
    resolver.seed_access_token(
        USER_ID, conn.credential_generation, "at-seeded-1", expires_in=3600
    )
    ctx = ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-wallet-1",
        user_name="Alice",
        user_id=USER_ID,
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=clock,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers={LINK_PROVIDER.name: resolver},
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )
    try:
        yield _WalletFixture(
            db=db,
            server=server,
            resolver=resolver,
            clock=clock,
            exec_context=ctx,
        )
    finally:
        await http_client.aclose()
        clear_browser_session_review_modes()
        clear_handed_back_scopes()


@pytest.mark.asyncio
async def test_wallet_status_tags_ineligible_cards_redacts_addresses_and_latches_checked(
    wallet: _WalletFixture,
) -> None:
    scope = await browser_scope_key(wallet.exec_context)
    initialize_browser_session_review_mode(scope, "unchecked")
    assert get_browser_session_review_mode(scope) == "unchecked"

    result = await link_wallet_status_tool(wallet.exec_context)
    data = result.get_data()
    assert isinstance(data, dict)

    assert data["default_currency"] == "aud"
    assert data["spend_limits"]["per_transaction"]["limit"] == 50000
    assert data["verification_requirement"]["status"] == "not_required"

    by_id = {pm["id"]: pm for pm in data["payment_methods"]}
    assert by_id["pm_eligible_1"]["eligible"] is True
    assert by_id["pm_eligible_1"]["brand"] == "visa"
    assert by_id["pm_eligible_1"]["last4"] == "4242"
    assert by_id["pm_restricted_2"]["eligible"] is False
    assert by_id["pm_restricted_2"]["ineligibility_reasons"] == ["unsupported_network"]
    assert by_id["pm_no_caps_3"]["eligible"] is True

    # Never exposes street address, phone, email, or PAN.
    serialized = json.dumps(data) + (result.text or "")
    assert "99 Secret Street" not in serialized
    assert "+61400000000" not in serialized
    assert "alice@example.com" not in serialized

    # Touching link_wallet_status latches the browser scope to checked.
    assert get_browser_session_review_mode(scope) == "checked"


@pytest.mark.asyncio
async def test_list_shipping_addresses_returns_addresses_and_latches_checked(
    wallet: _WalletFixture,
) -> None:
    scope = await browser_scope_key(wallet.exec_context)
    initialize_browser_session_review_mode(scope, "unchecked")

    result = await link_list_shipping_addresses_tool(wallet.exec_context)
    data = result.get_data()
    assert isinstance(data, dict)
    assert len(data["shipping_addresses"]) == 1
    assert data["shipping_addresses"][0]["address"]["line_1"] == "123 Collins St"
    assert get_browser_session_review_mode(scope) == "checked"

    descriptor = next(
        d for d in LOCAL_TOOL_DESCRIPTORS if d.name == "link_list_shipping_addresses"
    )
    assert ToolTag.READ_ONLY in descriptor.tags
    assert ToolTag.SENSITIVE_DATA in descriptor.tags


@pytest.mark.asyncio
async def test_create_spend_request_origin_binding_refuses_mismatch_and_http(
    wallet: _WalletFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Non-HTTPS merchant_url refused even with no browser session.
    http_res = await link_create_spend_request_tool(
        wallet.exec_context,
        amount=4500,
        currency="aud",
        context=LONG_CONTEXT,
        merchant_name="Example Roasters",
        merchant_url="http://shop.example.com/checkout",
    )
    assert http_res.get_data()["error"] == "invalid_merchant_url"  # type: ignore[index]
    assert wallet.server.recorded_requests == []

    # 2. Active browser on a different HTTPS origin -> refused mechanically.
    async def _evil_backend(_ctx: ToolExecutionContext) -> _FakeBrowserBackend:
        return _FakeBrowserBackend(current_url="https://phishing.example.net/pay")

    monkeypatch.setattr(link_wallet_mod, "get_existing_browser_backend", _evil_backend)
    mismatch_res = await link_create_spend_request_tool(
        wallet.exec_context,
        amount=4500,
        currency="aud",
        context=LONG_CONTEXT,
        merchant_name="Example Roasters",
        merchant_url="https://shop.example.com/checkout",
    )
    mismatch_data = mismatch_res.get_data()
    assert isinstance(mismatch_data, dict)
    assert mismatch_data["error"] == "merchant_url_origin_mismatch"
    assert mismatch_data["merchant_origin"] == "https://shop.example.com"
    assert mismatch_data["browser_origin"] == "https://phishing.example.net"
    assert wallet.server.recorded_requests == []

    # 3. Active browser on plaintext HTTP -> refused mechanically.
    async def _http_backend(_ctx: ToolExecutionContext) -> _FakeBrowserBackend:
        return _FakeBrowserBackend(current_url="http://shop.example.com/checkout")

    monkeypatch.setattr(link_wallet_mod, "get_existing_browser_backend", _http_backend)
    browser_http_res = await link_create_spend_request_tool(
        wallet.exec_context,
        amount=4500,
        currency="aud",
        context=LONG_CONTEXT,
        merchant_name="Example Roasters",
        merchant_url="https://shop.example.com/checkout",
    )
    assert browser_http_res.get_data()["error"] == "browser_not_https"  # type: ignore[index]
    assert wallet.server.recorded_requests == []

    # 4. Matching HTTPS origin (with explicit :443 normalization) succeeds!
    async def _matching_backend(_ctx: ToolExecutionContext) -> _FakeBrowserBackend:
        return _FakeBrowserBackend(current_url="https://SHOP.example.com/cart")

    monkeypatch.setattr(
        link_wallet_mod, "get_existing_browser_backend", _matching_backend
    )
    wallet.server.spend_create_response = _base_spend_request(status="approved")
    ok_res = await link_create_spend_request_tool(
        wallet.exec_context,
        amount=4500,
        currency="AUD",
        context=LONG_CONTEXT,
        merchant_name="Example Roasters",
        merchant_url="https://shop.example.com:443/checkout",
    )
    ok_data = ok_res.get_data()
    assert isinstance(ok_data, dict)
    assert ok_data["status"] == "approved"
    assert ok_data["spend_request_id"] == "sr_123"

    # Verify request payload used card mode, request_approval=True, fresh idempotency_key,
    # and never requested include=card.
    assert len(wallet.server.recorded_requests) == 1
    method, path, query, body = wallet.server.recorded_requests[0]
    assert (method, path, query) == ("POST", "/spend_requests", "")
    assert body is not None
    assert body["credential_type"] == "card"
    assert body["request_approval"] is True
    assert isinstance(body["idempotency_key"], str) and body["idempotency_key"]


@pytest.mark.asyncio
async def test_create_spend_request_polling_pending_and_requires_action_and_never_leaks_card(
    wallet: _WalletFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_browser(_ctx: ToolExecutionContext) -> None:
        return None

    async def _fast_wait(seconds: float) -> None:
        wallet.clock.advance(timedelta(seconds=seconds))

    monkeypatch.setattr(link_wallet_mod, "get_existing_browser_backend", _no_browser)
    monkeypatch.setattr(link_wallet_mod, "_wait_for_poll", _fast_wait)
    monkeypatch.setattr(link_wallet_mod, "_SPEND_REQUEST_WAIT_SECONDS", 4.0)
    monkeypatch.setattr(link_wallet_mod, "_SPEND_REQUEST_POLL_INTERVAL_SECONDS", 2.0)

    # Case A: stays pending_approval past wait budget -> returns approval_url & pause guidance.
    wallet.server.spend_create_response = _base_spend_request(
        sr_id="sr_pending_1",
        status="pending_approval",
        approval_url="https://link.com/approve/sr_pending_1",
    )
    wallet.server.spend_retrieve_responses = [
        _base_spend_request(
            sr_id="sr_pending_1",
            status="pending_approval",
            approval_url="https://link.com/approve/sr_pending_1",
        ),
        _base_spend_request(
            sr_id="sr_pending_1",
            status="pending_approval",
            approval_url="https://link.com/approve/sr_pending_1",
        ),
    ]
    scope = await browser_scope_key(wallet.exec_context)
    initialize_browser_session_review_mode(scope, "unchecked")

    pending_res = await link_create_spend_request_tool(
        wallet.exec_context,
        amount=4500,
        currency="aud",
        context=LONG_CONTEXT,
        merchant_name="Example Roasters",
        merchant_url="https://shop.example.com/checkout",
    )
    pending_data = pending_res.get_data()
    assert isinstance(pending_data, dict)
    assert pending_data["status"] == "pending_approval"
    assert pending_data["spend_request_id"] == "sr_pending_1"
    assert pending_data["approval_url"] == "https://link.com/approve/sr_pending_1"
    assert "stop" in (pending_res.text or "").lower()
    assert get_browser_session_review_mode(scope) == "checked"
    # None of the poll calls ever passed include=card.
    assert all("include=" not in req[2] for req in wallet.server.recorded_requests)

    # Case B: transitions from pending_approval to requires_action (auto_resume).
    wallet.server.spend_create_response = _base_spend_request(
        sr_id="sr_action_1",
        status="pending_approval",
        approval_url="https://link.com/approve/sr_action_1",
    )
    wallet.server.spend_retrieve_responses = [
        _base_spend_request(
            sr_id="sr_action_1",
            status="requires_action",
            status_details={
                "requires_action": {
                    "failure_code": "step_up_needed",
                    "next_action": {
                        "type": "identity_verification",
                        "resolution": "auto_resume",
                        "display_message": "Verify your identity in Link",
                        "action_url": "https://link.com/verify/123",
                    },
                }
            },
        )
    ]
    action_res = await link_create_spend_request_tool(
        wallet.exec_context,
        amount=4500,
        currency="aud",
        context=LONG_CONTEXT,
        merchant_name="Example Roasters",
        merchant_url="https://shop.example.com/checkout",
    )
    action_data = action_res.get_data()
    assert isinstance(action_data, dict)
    assert action_data["status"] == "requires_action"
    assert action_data["resolution"] == "auto_resume"
    assert action_data["display_message"] == "Verify your identity in Link"
    assert action_data["action_url"] == "https://link.com/verify/123"
    assert "link_check_spend_request" in (action_res.text or "")

    # Case C: even if an upstream response somehow included card fields, tool never emits them.
    resp_with_card = _base_spend_request(sr_id="sr_secret_card", status="approved")
    resp_with_card["card"] = {
        "id": "card_1",
        "brand": "visa",
        "exp_month": 12,
        "exp_year": 2030,
        "number": "4111111111114242",
        "cvc": "987",
    }
    wallet.server.spend_retrieve_responses = [resp_with_card]
    check_res = await link_check_spend_request_tool(
        wallet.exec_context, spend_request_id="sr_secret_card"
    )
    serialized = json.dumps(check_res.get_data()) + (check_res.text or "")
    assert "4111111111114242" not in serialized
    assert "987" not in serialized


@pytest.mark.asyncio
async def test_check_and_cancel_spend_request_and_scope_latching(
    wallet: _WalletFixture,
) -> None:
    scope = await browser_scope_key(wallet.exec_context)

    initialize_browser_session_review_mode(scope, "unchecked")
    wallet.server.spend_retrieve_responses = [
        _base_spend_request(
            sr_id="sr_paid_1",
            status="succeeded",
            payment_status_details={
                "outcome": "success",
                "amount": 4500,
                "currency": "aud",
            },
        )
    ]
    check_res = await link_check_spend_request_tool(
        wallet.exec_context, spend_request_id="sr_paid_1"
    )
    check_data = check_res.get_data()
    assert isinstance(check_data, dict)
    assert check_data["status"] == "succeeded"
    assert check_data["payment_status_details"]["outcome"] == "success"
    assert get_browser_session_review_mode(scope) == "checked"

    clear_browser_session_review_modes()
    initialize_browser_session_review_mode(scope, "unchecked")
    wallet.server.spend_cancel_response = _base_spend_request(
        sr_id="sr_cancel_1", status="canceled"
    )
    cancel_res = await link_cancel_spend_request_tool(
        wallet.exec_context, spend_request_id="sr_cancel_1"
    )
    assert cancel_res.get_data()["status"] == "canceled"  # type: ignore[index]
    assert get_browser_session_review_mode(scope) == "checked"


@pytest.mark.no_db
def test_defaults_yaml_browser_profiles_expose_link_tools_with_static_review(
    tmp_path: Path,
) -> None:
    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing.yaml"),
    )
    descriptors_by_name = {d.name: d for d in LOCAL_TOOL_DESCRIPTORS}

    for profile_id in ("browser_profile", "credential_browser_profile"):
        profile = next(p for p in config.service_profiles if p.id == profile_id)
        assert profile.tools_policy is not None
        engine = PolicyEngine.from_layers(
            defaults=profile.tools_policy,
            profile=config.global_tools_policy,
            operator=profile.operator_tools_policy,
        )
        for allow_tool in (
            "link_wallet_status",
            "link_list_shipping_addresses",
            "link_check_spend_request",
            "link_cancel_spend_request",
        ):
            decision = engine.evaluate(descriptors_by_name[allow_tool]).decision
            assert decision is ToolPolicyDecision.ALLOW, (profile_id, allow_tool)

        create_decision = engine.evaluate(
            descriptors_by_name["link_create_spend_request"]
        ).decision
        assert create_decision is ToolPolicyDecision.REVIEW, profile_id


@pytest.mark.no_db
def test_normalize_https_origin_edge_cases() -> None:
    assert normalize_https_origin("https://Shop.Example.COM:443/pay") == (
        "https://shop.example.com"
    )
    assert normalize_https_origin("https://shop.example.com:8443/pay") == (
        "https://shop.example.com:8443"
    )
    assert normalize_https_origin("http://shop.example.com/pay") is None
    assert normalize_https_origin("https://user:pass@shop.example.com/pay") is None
    assert normalize_https_origin("javascript:alert(1)") is None
    assert normalize_https_origin("about:blank") is None
