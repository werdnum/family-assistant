"""End-to-end ASGI integration tests for ``browser_fill_payment`` (Milestone 2).

Exercises ``browser_fill_payment_tool``, ``LinkWalletService.retrieve_spend_request_with_card``,
and ``RemoteBrowserBackend.fill_payment`` against:
- the real ``browser-server`` FastAPI ASGI app (``browser_handoff_service.main.app``),
- a fake Link API server (``httpx.MockTransport``), and
- a real SQLite database (``db_engine``) + ``OAuthCredentialResolver``.

Includes both deterministic ``FakeBrowserWorker`` tests and a real-Chromium
multi-origin Stripe-iframe checkout test (skipped cleanly if Chromium is unavailable).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from browser_handoff_service.main import app as browser_server_app
from browser_handoff_service.main import registry as browser_server_registry
from browser_handoff_service.runtime import (
    FakeBrowserWorker,
    PlaywrightBrowserWorker,
    RuntimeUnavailable,
)
from pydantic import SecretStr

from family_assistant.config_models import (
    BrowserHandoffConfig,
    LinkIntegrationConfig,
    RemoteA2AAuthConfig,
)
from family_assistant.services.credential_encryption import (
    CredentialEncryption,
    generate_key,
)
from family_assistant.services.link_provider import (
    LINK_DEFAULT_OAUTH_CLIENT_ID,
    LINK_PROVIDER,
    LinkScope,
)
from family_assistant.services.link_wallet import (
    LinkWalletError,
    LinkWalletService,
    RetrievedBillingAddress,
    RetrievedLinkCard,
)
from family_assistant.services.oauth_credentials import OAuthCredentialResolver
from family_assistant.storage.database import Database
from family_assistant.tools.browser_backend import (
    BrowserBackendError,
    LocalPlaywrightBackend,
    RemoteBrowserBackend,
    close_browser_backend,
    get_browser_backend,
)
from family_assistant.tools.browser_session import (
    BrowserSession,
    browser_scope_key,
    clear_browser_session_review_modes,
    clear_handed_back_scopes,
    get_browser_session_review_mode,
    initialize_browser_session_review_mode,
)
from family_assistant.tools.link_wallet import browser_fill_payment_tool
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_SERVICE_TOKEN = "integration-payment-token"
_SERVICE_URL = "http://browser-server.local"

MERCHANT = "https://shop.example.com"
STRIPE = "https://js.stripe.com"
EVIL = "https://evil-pay.example.net"
OTHER = "https://other.example.com"

USER_ID = "user-alice"
AGENTIC_SCOPE = LinkScope.PAYMENT_METHODS_AGENTIC.value
USERINFO_SCOPE = LinkScope.USERINFO_READ.value

PAN = "4242424242429876"
CVC = "9473"
EXP_MONTH = 4
EXP_YEAR = 2031
CARDHOLDER = "Alice Example"

_HANDOFF_PROFILES = [
    "browser_profile",
    "browser_visual_profile",
    "credential_browser_profile",
    "credential_browser_visual_profile",
]


def _make_card_payload(
    *,
    number: str = PAN,
    cvc: str | None = CVC,
    exp_month: int = EXP_MONTH,
    exp_year: int = EXP_YEAR,
    include_billing: bool = True,
) -> dict[str, object]:
    card: dict[str, object] = {
        "id": "vcard_1",
        "brand": "visa",
        "number": number,
        "exp_month": exp_month,
        "exp_year": exp_year,
    }
    if cvc is not None:
        card["cvc"] = cvc
    if include_billing:
        card["billing_address"] = {
            "name": CARDHOLDER,
            "line1": "123 Collins St",
            "line2": "Apt 4B",
            "city": "Melbourne",
            "state": "VIC",
            "postal_code": "3000",
            "country": "AU",
        }
    return card


def _make_spend_request(
    *,
    sr_id: str = "sr_approved_1",
    status: str = "approved",
    merchant_url: str | None = f"{MERCHANT}/checkout",
    approval_url: str | None = None,
    status_details: dict[str, object] | None = None,
    card: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": sr_id,
        "merchant_name": "Example Roasters",
        "merchant_url": merchant_url,
        "amount": 4500,
        "currency": "aud",
        "status": status,
        "card_brand": "visa",
        "card_last4": "9876",
        "created_at": "2026-11-24T12:00:00Z",
        "updated_at": "2026-11-24T12:00:01Z",
    }
    if approval_url is not None:
        payload["approval_url"] = approval_url
    if status_details is not None:
        payload["status_details"] = status_details
    if card is not None:
        payload["card"] = card
    return payload


@dataclass
class _FakeLinkApiServer:
    """Stubs api.link.com spend_requests.retrieve with strict include=['card'] tracking."""

    spend_requests: dict[str, dict[str, object]] = field(default_factory=dict)
    cards_by_id: dict[str, dict[str, object] | None] = field(default_factory=dict)
    raw_override_on_card_fetch: httpx.Response | None = None
    recorded_requests: list[tuple[str, str, dict[str, list[str]]]] = field(
        default_factory=list
    )

    def _handler(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        qs = parse_qs(parsed.query)
        self.recorded_requests.append((request.method, parsed.path, qs))

        if request.method == "GET" and parsed.path.startswith("/spend_requests/"):
            sr_id = parsed.path.removeprefix("/spend_requests/")
            include_values = qs.get("include", []) + qs.get("include[]", [])
            wants_card = any("card" in v for v in include_values)
            if wants_card and self.raw_override_on_card_fetch is not None:
                return self.raw_override_on_card_fetch
            base = self.spend_requests.get(sr_id)
            if base is None:
                return httpx.Response(404, json={"error": "not_found"})
            body = dict(base)
            if wants_card:
                card = self.cards_by_id.get(sr_id)
                if card is not None:
                    body["card"] = card
            return httpx.Response(200, json=body)

        return httpx.Response(404, json={"error": "unknown_route"})

    def card_fetch_requests(self) -> list[tuple[str, str, dict[str, list[str]]]]:
        return [
            req
            for req in self.recorded_requests
            if any(
                "card" in val
                for vals in (req[2].get("include", []) + req[2].get("include[]", []))
                for val in (vals if isinstance(vals, list) else [vals])
            )
        ]


async def _clear_browser_server_state() -> None:
    for session in list(browser_server_registry.list_sessions()):
        await browser_server_registry.close(session.session_id)
    browser_server_registry.sessions.clear()
    browser_server_registry.locks.clear()
    browser_server_registry.events.clear()
    browser_server_registry.tokens.clear()
    browser_server_registry.workers.clear()


@pytest.fixture(autouse=True)
async def _browser_server_state(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    monkeypatch.setenv("BROWSER_RUNTIME", "fake")
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", _SERVICE_TOKEN)

    orig_init = RemoteBrowserBackend.__init__

    def _patched_init(
        self: RemoteBrowserBackend,
        config: BrowserHandoffConfig,
        conversation_id: str,
        client: httpx.AsyncClient | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        if client is None:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=browser_server_app),
                timeout=config.timeout_seconds,
            )
        orig_init(self, config, conversation_id, client=client, **kwargs)

    monkeypatch.setattr(RemoteBrowserBackend, "__init__", _patched_init)
    clear_browser_session_review_modes()
    clear_handed_back_scopes()
    await _clear_browser_server_state()
    yield
    await _clear_browser_server_state()
    clear_browser_session_review_modes()
    clear_handed_back_scopes()


def _handoff_config() -> BrowserHandoffConfig:
    return BrowserHandoffConfig(
        enabled=True,
        service_url=_SERVICE_URL,
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
        handoff_capable_profiles=list(_HANDOFF_PROFILES),
        autofill_capable_profiles=[
            "credential_browser_profile",
            "credential_browser_visual_profile",
        ],
    )


@dataclass
class _PaymentHarness:
    db: Database
    link_server: _FakeLinkApiServer
    resolver: OAuthCredentialResolver
    clock: MockClock

    def make_context(
        self,
        *,
        conversation_id: str = "conv-link-pay-1",
        profile_id: str = "browser_profile",
    ) -> ToolExecutionContext:
        processing_service = SimpleNamespace(
            app_config=SimpleNamespace(
                authenticated_sites={},
                browser_handoff_config=_handoff_config(),
            )
        )
        return ToolExecutionContext(
            interface_type="web",
            conversation_id=conversation_id,
            user_name="Alice",
            user_id=USER_ID,
            turn_id="turn-pay-1",
            db_context=self.db,
            processing_service=cast("Any", processing_service),
            clock=self.clock,
            plugins=None,
            event_sources=None,
            attachment_registry=None,
            credential_resolvers={LINK_PROVIDER.name: self.resolver},
            api_backend=None,
            timezone=ZoneInfo("UTC"),
            processing_profile_id=profile_id,
        )


@pytest_asyncio.fixture
async def harness(db_engine: AsyncEngine) -> AsyncGenerator[_PaymentHarness]:
    key = generate_key()
    encryption = CredentialEncryption(key)
    clock = MockClock(datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    link_server = _FakeLinkApiServer()
    # Dedicated httpx client for Link API (with explicit MockTransport, unaffected by
    # the autouse ASGITransport default used by RemoteBrowserBackend).
    link_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(link_server._handler)
    )
    cfg = LinkIntegrationConfig.model_validate({
        "enabled": True,
        "oauth_client_id": LINK_DEFAULT_OAUTH_CLIENT_ID,
        "oauth_client_secret": "",
        "credential_encryption_key": key,
        "scopes": [AGENTIC_SCOPE, USERINFO_SCOPE],
        "require_taint_enforcement": False,
    })
    resolver = OAuthCredentialResolver(
        LINK_PROVIDER, cfg, encryption, link_http_client, notifier=None, clock=clock
    )
    db = Database(engine=db_engine)
    conn = await db.oauth_connections.upsert_connection(
        user_id=USER_ID,
        provider=LINK_PROVIDER.name,
        provider_account_email="alice@example.com",
        scopes=[AGENTIC_SCOPE, USERINFO_SCOPE],
        refresh_token_encrypted=encryption.encrypt("rt-pay-1"),
    )
    resolver.seed_access_token(
        USER_ID, conn.credential_generation, "at-pay-seeded", expires_in=3600
    )
    try:
        yield _PaymentHarness(
            db=db, link_server=link_server, resolver=resolver, clock=clock
        )
    finally:
        await link_http_client.aclose()


def _session_record(session_id: str) -> Any:  # noqa: ANN401 - browser-server model
    return browser_server_registry.sessions[session_id]


def _fake_worker(session_id: str) -> FakeBrowserWorker:
    return cast(
        "FakeBrowserWorker",
        browser_server_registry.workers[_session_record(session_id).worker_id],
    )


def _configure_stripe_checkout_fields(worker: FakeBrowserWorker) -> None:
    """Populate a realistic Stripe Elements cross-origin iframe + top-frame billing checkout."""
    worker.payment_fields = [
        {
            "ref": None,
            "kind": "card_number",
            "frame_origin": STRIPE,
            "autocomplete": "cc-number",
            "tag": "input",
            "input_type": "text",
        },
        {
            "ref": None,
            "kind": "card_exp",
            "frame_origin": STRIPE,
            "autocomplete": "cc-exp",
            "tag": "input",
            "input_type": "text",
            "placeholder": "MM / YY",
        },
        {
            "ref": None,
            "kind": "card_cvc",
            "frame_origin": STRIPE,
            "autocomplete": "cc-csc",
            "tag": "input",
            "input_type": "text",
        },
        {
            "ref": "e10",
            "kind": "card_name",
            "frame_origin": MERCHANT,
            "autocomplete": "cc-name",
            "name": "Name on card",
            "tag": "input",
            "input_type": "text",
        },
        {
            "ref": "e11",
            "kind": "billing_postal_code",
            "frame_origin": MERCHANT,
            "autocomplete": "billing postal-code",
            "name": "Billing postal code",
            "tag": "input",
            "input_type": "text",
        },
        {
            "ref": "e12",
            "kind": "billing_country",
            "frame_origin": MERCHANT,
            "autocomplete": "billing country",
            "name": "Billing country",
            "tag": "select",
            "options": [
                {"value": "", "label": "Select"},
                {"value": "AU", "label": "Australia"},
                {"value": "US", "label": "United States"},
            ],
        },
    ]


def _assert_no_card_leak(text: str) -> None:
    assert PAN not in text
    assert CVC not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_id",
    [
        "browser_profile",
        "credential_browser_profile",
        "browser_visual_profile",
        "credential_browser_visual_profile",
    ],
)
async def test_fill_payment_happy_path_and_post_fill_readback_protection(
    harness: _PaymentHarness,
    caplog: pytest.LogCaptureFixture,
    profile_id: str,
) -> None:
    """Fills cross-origin Stripe iframes + top-frame billing, latches readback protection and checked scope."""
    caplog.set_level(logging.DEBUG)
    harness.link_server.spend_requests["sr_ok"] = _make_spend_request(sr_id="sr_ok")
    harness.link_server.cards_by_id["sr_ok"] = _make_card_payload()

    ctx = harness.make_context(
        conversation_id=f"conv-happy-{profile_id}", profile_id=profile_id
    )
    scope = await browser_scope_key(ctx)
    initialize_browser_session_review_mode(scope, "unchecked")

    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        await backend.goto(f"{MERCHANT}/checkout")
        assert backend.session_id is not None
        record = _session_record(backend.session_id)
        worker = _fake_worker(backend.session_id)
        _configure_stripe_checkout_fields(worker)

        is_ordinary = not profile_id.startswith("credential_")
        if is_ordinary:
            assert record.autofill_enabled is False
            assert record.readback_protected is False
            # Ordinary sessions permit exec and extract before payment fill.
            assert await backend.evaluate("document.title")
            assert "<h1>" in await backend.extract_html(None)
        else:
            assert record.autofill_enabled is True
            assert record.readback_protected is True

        # Capture a screenshot before fill so we verify invalidation.
        await backend.capture_screenshot()
        assert backend.last_screenshot is not None

        result = await browser_fill_payment_tool(ctx, spend_request_id="sr_ok")
        data = result.get_data()
        assert isinstance(data, dict)
        assert set(data.keys()) == {"status", "origin", "filled_fields", "reason"}
        assert data["status"] == "filled", result.get_text()
        assert data["origin"] == MERCHANT
        assert data["reason"] is None

        by_kind = {item["kind"]: item for item in data["filled_fields"]}
        assert by_kind["card_number"]["frame_origin"] == STRIPE
        assert by_kind["card_exp"]["frame_origin"] == STRIPE
        assert by_kind["card_cvc"]["frame_origin"] == STRIPE
        assert by_kind["card_name"]["frame_origin"] == MERCHANT
        assert by_kind["billing_postal_code"]["frame_origin"] == MERCHANT
        assert by_kind["billing_country"]["frame_origin"] == MERCHANT

        # Verify worker actually received the expected virtual card values.
        worker_by_kind = {item["kind"]: item for item in worker.payment_filled}
        assert worker_by_kind["card_number"]["value"] == PAN
        assert worker_by_kind["card_exp"]["value"] == "04/31"
        assert worker_by_kind["card_cvc"]["value"] == CVC
        assert worker_by_kind["card_name"]["value"] == CARDHOLDER
        assert worker_by_kind["billing_postal_code"]["value"] == "3000"
        assert worker_by_kind["billing_country"]["value"] == "AU"

        # Screenshot cache was invalidated on fill.
        assert backend.last_screenshot is None
        assert backend.last_screenshot_invalidation_reason == "payment filled"

        # Browser scope was latched to checked.
        assert get_browser_session_review_mode(scope) == "checked"

        # Exactly 1 metadata retrieve and 1 include=['card'] retrieve occurred.
        assert len(harness.link_server.recorded_requests) == 2
        assert len(harness.link_server.card_fetch_requests()) == 1

        # Post-fill readback protection is latched on browser-server, while
        # autofill_enabled remains False on ordinary sessions.
        assert record.readback_protected is True
        assert record.allow_exec is False
        assert worker.mask_protected is True
        assert record.autofill_enabled is (not is_ordinary)
        assert backend.autofill_enabled is (not is_ordinary)

        with pytest.raises(BrowserBackendError, match="denied"):
            await backend.evaluate("1 + 1")
        with pytest.raises(BrowserBackendError, match="denied"):
            await backend.extract_html(None)
        with pytest.raises(BrowserBackendError, match="denied"):
            await backend.keyboard_press("Control+c")

        # Snapshot masks all protected fields and leaks no PAN/CVC/cardholder.
        snap = await backend.raw_snapshot(1)
        snap_json = json.dumps(snap)
        _assert_no_card_leak(snap_json)
        assert CARDHOLDER not in snap_json

        # In an ordinary session, Keychute autofill remains unavailable.
        if is_ordinary:
            af = await backend.autofill(step_key="k1", secret_name="some-login")
            assert af["status"] == "refused"
            assert af["reason"] == "autofill_disabled"

        # Zero PAN/CVC anywhere in tool output, logs, or browser-server state.
        _assert_no_card_leak(result.get_text())
        _assert_no_card_leak(json.dumps(data))
        _assert_no_card_leak(caplog.text)
        _assert_no_card_leak(json.dumps(record.model_dump(mode="json")))
        _assert_no_card_leak(
            json.dumps([
                ev.model_dump(mode="json")
                for ev in browser_server_registry.events.get(backend.session_id, [])
            ])
        )
    finally:
        await close_browser_backend(ctx)


@pytest.mark.asyncio
async def test_fill_payment_without_billing_address_omits_billing_fields(
    harness: _PaymentHarness,
) -> None:
    harness.link_server.spend_requests["sr_no_bill"] = _make_spend_request(
        sr_id="sr_no_bill"
    )
    harness.link_server.cards_by_id["sr_no_bill"] = _make_card_payload()
    ctx = harness.make_context(conversation_id="conv-no-bill")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        await backend.goto(f"{MERCHANT}/checkout")
        assert backend.session_id is not None
        worker = _fake_worker(backend.session_id)
        _configure_stripe_checkout_fields(worker)

        result = await browser_fill_payment_tool(
            ctx, spend_request_id="sr_no_bill", include_billing_address=False
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["status"] == "filled"
        kinds = {item["kind"] for item in data["filled_fields"]}
        assert {"card_number", "card_exp", "card_cvc", "card_name"} <= kinds
        assert "billing_postal_code" not in kinds
        assert "billing_country" not in kinds
    finally:
        await close_browser_backend(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "approval_url", "status_details"),
    [
        ("pending_approval", "https://link.com/approve/sr_unapp", None),
        (
            "requires_action",
            None,
            {
                "requires_action": {
                    "failure_code": "verification_required",
                    "next_action": {
                        "type": "verify_identity",
                        "resolution": "auto_resume",
                        "display_message": "Verify identity in Link",
                        "action_url": "https://link.com/action/1",
                    },
                }
            },
        ),
        ("denied", None, None),
        ("canceled", None, None),
        ("expired", None, None),
    ],
)
async def test_unapproved_spend_request_never_fetches_card(
    harness: _PaymentHarness,
    status: str,
    approval_url: str | None,
    status_details: dict[str, object] | None,
) -> None:
    harness.link_server.spend_requests["sr_unapp"] = _make_spend_request(
        sr_id="sr_unapp",
        status=status,
        approval_url=approval_url,
        status_details=status_details,
    )
    harness.link_server.cards_by_id["sr_unapp"] = _make_card_payload()

    ctx = harness.make_context(conversation_id=f"conv-unapp-{status}")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        await backend.goto(f"{MERCHANT}/checkout")
        result = await browser_fill_payment_tool(ctx, spend_request_id="sr_unapp")
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["status"] == "refused"
        assert data["reason"] == "spend_request_not_approved"
        assert data["filled_fields"] == []
        # CRITICAL: include=['card'] was NEVER requested!
        assert harness.link_server.card_fetch_requests() == []
        assert len(harness.link_server.recorded_requests) == 1
        if status == "pending_approval":
            assert "https://link.com/approve/sr_unapp" in result.get_text()
        if status == "requires_action":
            assert "https://link.com/action/1" in result.get_text()
    finally:
        await close_browser_backend(ctx)


@pytest.mark.asyncio
async def test_wrong_origin_caught_before_card_fetch(
    harness: _PaymentHarness,
) -> None:
    harness.link_server.spend_requests["sr_shop"] = _make_spend_request(
        sr_id="sr_shop", merchant_url=f"{MERCHANT}/checkout"
    )
    harness.link_server.cards_by_id["sr_shop"] = _make_card_payload()

    ctx = harness.make_context(conversation_id="conv-wrong-origin-precheck")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        await backend.goto(f"{EVIL}/checkout")
        result = await browser_fill_payment_tool(ctx, spend_request_id="sr_shop")
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["status"] == "refused"
        assert data["reason"] == "wrong_origin"
        assert data["origin"] == EVIL
        # CRITICAL: origin mismatch refused before ever requesting include=['card']!
        assert harness.link_server.card_fetch_requests() == []
        assert len(harness.link_server.recorded_requests) == 1
    finally:
        await close_browser_backend(ctx)


@pytest.mark.asyncio
async def test_not_found_and_prevalidation_refusals_never_fetch_card(
    harness: _PaymentHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = harness.make_context(conversation_id="conv-preval")

    # 1. No active browser session yet -> refused before calling Link.
    no_session = await browser_fill_payment_tool(ctx, spend_request_id="sr_missing")
    assert no_session.get_data()["reason"] == "remote_browser_required"  # type: ignore[index]
    assert harness.link_server.recorded_requests == []

    # 2. Invalid spend_request_id / invalid field_refs -> refused before calling Link.
    bad_id = await browser_fill_payment_tool(ctx, spend_request_id="   ")
    assert bad_id.get_data()["reason"] == "invalid_spend_request_id"  # type: ignore[index]

    bad_ref = await browser_fill_payment_tool(
        ctx,
        spend_request_id="sr_ok",
        field_refs=[{"ref": "not-a-ref", "kind": "card_number"}],
    )
    assert bad_ref.get_data()["reason"] == "invalid_ref"  # type: ignore[index]

    bad_kind = await browser_fill_payment_tool(
        ctx,
        spend_request_id="sr_ok",
        field_refs=[{"ref": "e10", "kind": "ssn"}],
    )
    assert bad_kind.get_data()["reason"] == "invalid_ref"  # type: ignore[index]

    dup_ref = await browser_fill_payment_tool(
        ctx,
        spend_request_id="sr_ok",
        field_refs=[
            {"ref": "e10", "kind": "card_number"},
            {"ref": "e10", "kind": "card_cvc"},
        ],
    )
    assert dup_ref.get_data()["reason"] == "ambiguous_fields"  # type: ignore[index]
    assert harness.link_server.recorded_requests == []

    # 3. Local Playwright backend active -> refused before calling Link.
    local_backend = LocalPlaywrightBackend(BrowserSession())

    async def _return_local(_ctx: ToolExecutionContext) -> LocalPlaywrightBackend:
        return local_backend

    with monkeypatch.context() as mp:
        mp.setattr(
            "family_assistant.tools.link_wallet.get_existing_browser_backend",
            _return_local,
        )
        local_res = await browser_fill_payment_tool(ctx, spend_request_id="sr_ok")
        assert local_res.get_data()["reason"] == "remote_browser_required"  # type: ignore[index]
        assert harness.link_server.recorded_requests == []

    # 4. Plaintext HTTP browser page -> refused insecure_origin before calling Link.
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        await backend.goto("http://shop.example.com/checkout")
        http_res = await browser_fill_payment_tool(ctx, spend_request_id="sr_missing")
        assert http_res.get_data()["reason"] == "insecure_origin"  # type: ignore[index]
        assert harness.link_server.recorded_requests == []

        # 5. HTTPS page but spend request 404 -> refused not_found, never fetches card.
        await backend.goto(f"{MERCHANT}/checkout")
        not_found_res = await browser_fill_payment_tool(
            ctx, spend_request_id="sr_missing"
        )
        assert not_found_res.get_data()["reason"] == "not_found"  # type: ignore[index]
        assert harness.link_server.card_fetch_requests() == []
    finally:
        await close_browser_backend(ctx)


@pytest.mark.asyncio
async def test_untrusted_payment_frame_refusal_from_browser_server(
    harness: _PaymentHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    harness.link_server.spend_requests["sr_untrusted"] = _make_spend_request(
        sr_id="sr_untrusted"
    )
    harness.link_server.cards_by_id["sr_untrusted"] = _make_card_payload()

    ctx = harness.make_context(conversation_id="conv-untrusted-frame")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        await backend.goto(f"{MERCHANT}/checkout")
        assert backend.session_id is not None
        worker = _fake_worker(backend.session_id)
        worker.payment_fields = [
            {
                "ref": None,
                "kind": "card_number",
                "frame_origin": EVIL,
                "autocomplete": "cc-number",
                "tag": "input",
                "input_type": "text",
            },
            {
                "ref": None,
                "kind": "card_exp",
                "frame_origin": EVIL,
                "autocomplete": "cc-exp",
                "tag": "input",
                "input_type": "text",
            },
            {
                "ref": None,
                "kind": "card_cvc",
                "frame_origin": EVIL,
                "autocomplete": "cc-csc",
                "tag": "input",
                "input_type": "text",
            },
        ]

        result = await browser_fill_payment_tool(ctx, spend_request_id="sr_untrusted")
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["status"] == "refused"
        assert data["reason"] == "untrusted_payment_frame"
        assert "browser_request_handoff" in result.get_text()
        assert worker.payment_filled == []
        _assert_no_card_leak(result.get_text())
        _assert_no_card_leak(json.dumps(data))
        _assert_no_card_leak(caplog.text)
    finally:
        await close_browser_backend(ctx)


@pytest.mark.asyncio
async def test_server_side_origin_mismatch_and_mid_fill_navigation_refusals(
    harness: _PaymentHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    harness.link_server.spend_requests["sr_nav"] = _make_spend_request(sr_id="sr_nav")
    harness.link_server.cards_by_id["sr_nav"] = _make_card_payload()

    ctx = harness.make_context(conversation_id="conv-nav-race")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        await backend.goto(f"{MERCHANT}/checkout")
        assert backend.session_id is not None
        worker = _fake_worker(backend.session_id)
        _configure_stripe_checkout_fields(worker)

        # Case 1: Page navigated out-of-band without FA updating _last_url ->
        # browser-server catches wrong_origin server-side.
        worker.url = f"{OTHER}/checkout"
        wrong_res = await browser_fill_payment_tool(ctx, spend_request_id="sr_nav")
        assert wrong_res.get_data()["status"] == "refused"  # type: ignore[index]
        assert wrong_res.get_data()["reason"] == "wrong_origin"  # type: ignore[index]
        assert worker.payment_filled == []

        # Case 2: Page navigates mid-fill -> browser-server aborts with target_invalidated.
        worker.url = f"{MERCHANT}/checkout"

        def _navigate_mid_fill(_target: dict[str, object]) -> None:
            worker.url = f"{OTHER}/redirected"

        worker.on_payment_fill = _navigate_mid_fill
        inv_res = await browser_fill_payment_tool(ctx, spend_request_id="sr_nav")
        assert inv_res.get_data()["status"] == "refused"  # type: ignore[index]
        assert inv_res.get_data()["reason"] == "target_invalidated"  # type: ignore[index]
        assert "Re-snapshot" in inv_res.get_text()
        _assert_no_card_leak(inv_res.get_text())
        _assert_no_card_leak(caplog.text)
    finally:
        await close_browser_backend(ctx)


@pytest.mark.asyncio
async def test_ambiguous_fields_then_explicit_field_refs_override(
    harness: _PaymentHarness,
) -> None:
    harness.link_server.spend_requests["sr_ambig"] = _make_spend_request(
        sr_id="sr_ambig"
    )
    harness.link_server.cards_by_id["sr_ambig"] = _make_card_payload()

    ctx = harness.make_context(conversation_id="conv-ambig")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        await backend.goto(f"{MERCHANT}/checkout")
        assert backend.session_id is not None
        worker = _fake_worker(backend.session_id)
        worker.payment_fields = [
            {
                "ref": "e20",
                "kind": "card_number",
                "frame_origin": MERCHANT,
                "autocomplete": "cc-number",
                "tag": "input",
                "input_type": "text",
            },
            {
                "ref": "e21",
                "kind": "card_number",
                "frame_origin": MERCHANT,
                "autocomplete": "cc-number",
                "tag": "input",
                "input_type": "text",
            },
            {
                "ref": "e22",
                "kind": "card_exp",
                "frame_origin": MERCHANT,
                "autocomplete": "cc-exp",
                "tag": "input",
                "input_type": "text",
            },
            {
                "ref": "e23",
                "kind": "card_cvc",
                "frame_origin": MERCHANT,
                "autocomplete": "cc-csc",
                "tag": "input",
                "input_type": "text",
            },
        ]

        # Auto-detect refuses due to two card_number inputs.
        ambig = await browser_fill_payment_tool(ctx, spend_request_id="sr_ambig")
        assert ambig.get_data()["status"] == "refused"  # type: ignore[index]
        assert ambig.get_data()["reason"] == "ambiguous_fields"  # type: ignore[index]
        assert "field_refs" in ambig.get_text()

        # Retrying with explicit field_refs succeeds and fills the exact refs.
        ok = await browser_fill_payment_tool(
            ctx,
            spend_request_id="sr_ambig",
            field_refs=[
                {"ref": "e20", "kind": "card_number"},
                {"ref": "e22", "kind": "card_exp"},
                {"ref": "e23", "kind": "card_cvc"},
            ],
        )
        ok_data = ok.get_data()
        assert isinstance(ok_data, dict)
        assert ok_data["status"] == "filled", ok.get_text()
        filled_refs = {
            entry["kind"]: entry.get("ref") for entry in ok_data["filled_fields"]
        }
        assert filled_refs == {
            "card_number": "e20",
            "card_exp": "e22",
            "card_cvc": "e23",
        }
    finally:
        await close_browser_backend(ctx)


@pytest.mark.asyncio
async def test_secret_str_and_error_paths_never_leak_pan_or_cvc(
    harness: _PaymentHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify SecretStr masking plus zero-leak error handling on Link & browser-server failures."""
    caplog.set_level(logging.DEBUG)

    # 1. RetrievedLinkCard masks PAN and CVC in repr, str, model_dump, and JSON.
    card = RetrievedLinkCard(
        number=SecretStr(PAN),
        exp_month=EXP_MONTH,
        exp_year=EXP_YEAR,
        cvc=SecretStr(CVC),
        cardholder_name=CARDHOLDER,
        billing_address=RetrievedBillingAddress(
            name=CARDHOLDER, postal_code="3000", country="AU"
        ),
    )
    _assert_no_card_leak(repr(card))
    _assert_no_card_leak(str(card))
    _assert_no_card_leak(str(card.model_dump()))
    _assert_no_card_leak(card.model_dump_json())

    # 2. Malformed response from Link on include=['card'] (carrying PAN/CVC in body)
    # raises a redacted LinkWalletError with no __cause__ and no card bytes in logs.
    harness.link_server.raw_override_on_card_fetch = httpx.Response(
        200,
        content=f'{{"id":"sr_bad","card":{{"number":"{PAN}","cvc":"{CVC}"}},"bad":'.encode(),
        headers={"content-type": "application/json"},
    )
    ctx = harness.make_context(conversation_id="conv-leak-test")
    service = LinkWalletService.for_context(ctx)
    with pytest.raises(LinkWalletError) as exc_info:
        await service.retrieve_spend_request_with_card("sr_bad")
    assert exc_info.value.__cause__ is None
    _assert_no_card_leak(str(exc_info.value))
    _assert_no_card_leak(caplog.text)
    harness.link_server.raw_override_on_card_fetch = None

    # 3. RemoteBrowserBackend.fill_payment transport error & 422 error never leak PAN/CVC.
    def _failing_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/fill-payment"):
            raise httpx.ConnectError("connection dropped", request=request)
        return httpx.Response(200, json={"session_id": "bs_err"})

    err_client = httpx.AsyncClient(transport=httpx.MockTransport(_failing_transport))
    err_backend = RemoteBrowserBackend(_handoff_config(), "conv-err", client=err_client)
    err_backend.adopt_session("bs_err")
    with pytest.raises(BrowserBackendError) as be_exc:
        await err_backend.fill_payment(expected_origin=MERCHANT, card=card)
    assert be_exc.value.__cause__ is None
    _assert_no_card_leak(str(be_exc.value))
    await err_client.aclose()

    def _echo_422_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "card", "number"],
                        "msg": "invalid",
                        "input": PAN,
                    }
                ]
            },
        )

    v422_client = httpx.AsyncClient(transport=httpx.MockTransport(_echo_422_transport))
    v422_backend = RemoteBrowserBackend(
        _handoff_config(), "conv-422", client=v422_client
    )
    v422_backend.adopt_session("bs_422")
    with pytest.raises(BrowserBackendError) as v422_exc:
        await v422_backend.fill_payment(expected_origin=MERCHANT, card=card)
    _assert_no_card_leak(str(v422_exc.value))
    _assert_no_card_leak(caplog.text)
    await v422_client.aclose()


def _system_chrome_path() -> str | None:
    if os.environ.get("BROWSER_CHROMIUM_PATH"):
        return None
    candidate = "/usr/bin/google-chrome"
    return candidate if os.path.exists(candidate) else None


@pytest.mark.asyncio
async def test_real_chrome_multi_origin_stripe_iframe_end_to_end(
    harness: _PaymentHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real Chromium + real browser-server ASGI + LinkWalletService end-to-end test."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("BROWSER_RUNTIME", "playwright")
    chrome_path = _system_chrome_path()
    if chrome_path is not None:
        monkeypatch.setenv("BROWSER_CHROMIUM_PATH", chrome_path)

    harness.link_server.spend_requests["sr_chrome"] = _make_spend_request(
        sr_id="sr_chrome"
    )
    harness.link_server.cards_by_id["sr_chrome"] = _make_card_payload()

    pages = {
        f"{MERCHANT}/checkout": f"""<!doctype html><title>Real Checkout</title>
        <form id="checkout">
          <label for="cname">Name on card</label>
          <input id="cname" name="cardholderName" autocomplete="cc-name" type="text">
          <iframe id="f-num" src="{STRIPE}/elements/number" width="300" height="40"></iframe>
          <iframe id="f-exp" src="{STRIPE}/elements/exp" width="150" height="40"></iframe>
          <iframe id="f-cvc" src="{STRIPE}/elements/cvc" width="150" height="40"></iframe>
          <label for="zip">Billing postal code</label>
          <input id="zip" name="billing_zip" autocomplete="billing postal-code" type="text">
          <label for="country">Billing country</label>
          <select id="country" name="billing_country" autocomplete="billing country">
            <option value="">Select</option>
            <option value="AU">Australia</option>
            <option value="US">United States</option>
          </select>
          <button type="submit">Pay</button>
        </form>""",
        f"{STRIPE}/elements/number": (
            '<!doctype html><input id="cc-num" name="cardnumber" autocomplete="cc-number" '
            'data-elements-stable-field-name="cardNumber" type="text">'
        ),
        f"{STRIPE}/elements/exp": (
            '<!doctype html><input id="cc-exp" name="exp-date" autocomplete="cc-exp" '
            'placeholder="MM / YY" type="text">'
        ),
        f"{STRIPE}/elements/cvc": (
            '<!doctype html><input id="cc-cvc" name="cvc" autocomplete="cc-csc" type="text">'
        ),
    }

    ctx = harness.make_context(
        conversation_id="conv-real-chrome-pay", profile_id="browser_profile"
    )
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    try:
        try:
            # Provision the real Playwright worker first so we can mount route interception.
            await backend.goto("about:blank")
        except BrowserBackendError as exc:
            if "503" in str(exc) or "unavailable" in str(exc).lower():
                pytest.skip(f"real Chromium unavailable on this host: {exc}")
            raise
        except RuntimeUnavailable as exc:
            pytest.skip(f"real Chromium unavailable on this host: {exc}")

        assert backend.session_id is not None
        record = _session_record(backend.session_id)
        real_worker = cast(
            "PlaywrightBrowserWorker",
            browser_server_registry.workers[record.worker_id],
        )
        assert real_worker._context is not None

        async def _route_handler(route: Any) -> None:  # noqa: ANN401 - playwright Route
            html = pages.get(route.request.url)
            if html is not None:
                await route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                )
                return
            await route.abort()

        await real_worker._context.route("**/*", _route_handler)
        await backend.goto(f"{MERCHANT}/checkout")
        assert real_worker._page is not None
        for frame in real_worker._page.frames:
            await frame.wait_for_load_state("domcontentloaded")
        assert len(real_worker._page.frames) >= 4

        # Before fill: exec works in ordinary browser_profile session.
        assert await backend.evaluate("document.title") == "Real Checkout"

        result = await browser_fill_payment_tool(ctx, spend_request_id="sr_chrome")
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["status"] == "filled", result.get_text()
        assert data["origin"] == MERCHANT

        by_kind = {item["kind"]: item for item in data["filled_fields"]}
        assert by_kind["card_number"]["frame_origin"] == STRIPE
        assert by_kind["card_exp"]["frame_origin"] == STRIPE
        assert by_kind["card_cvc"]["frame_origin"] == STRIPE
        assert by_kind["card_name"]["frame_origin"] == MERCHANT
        assert by_kind["billing_postal_code"]["frame_origin"] == MERCHANT
        assert by_kind["billing_country"]["frame_origin"] == MERCHANT

        page = real_worker._page
        assert await page.locator("#cname").input_value() == CARDHOLDER
        assert await page.locator("#cname").get_attribute("data-fa-protected") == "true"
        assert await page.locator("#zip").input_value() == "3000"
        assert await page.locator("#zip").get_attribute("data-fa-protected") == "true"
        assert await page.locator("#country").input_value() == "AU"
        assert (
            await page.locator("#country").get_attribute("data-fa-protected") == "true"
        )

        num_frame = page.frame(url=f"{STRIPE}/elements/number")
        exp_frame = page.frame(url=f"{STRIPE}/elements/exp")
        cvc_frame = page.frame(url=f"{STRIPE}/elements/cvc")
        assert num_frame is not None and exp_frame is not None and cvc_frame is not None
        assert await num_frame.locator("#cc-num").input_value() == PAN
        assert (
            await num_frame.locator("#cc-num").get_attribute("data-fa-protected")
            == "true"
        )
        assert await exp_frame.locator("#cc-exp").input_value() == "04/31"
        assert (
            await exp_frame.locator("#cc-exp").get_attribute("data-fa-protected")
            == "true"
        )
        assert await cvc_frame.locator("#cc-cvc").input_value() == CVC
        assert (
            await cvc_frame.locator("#cc-cvc").get_attribute("data-fa-protected")
            == "true"
        )

        # Post-fill readback protection in real Chrome.
        with pytest.raises(BrowserBackendError, match="denied"):
            await backend.evaluate("document.title")
        with pytest.raises(BrowserBackendError, match="denied"):
            await backend.extract_html(None)
        with pytest.raises(BrowserBackendError, match="denied"):
            await backend.keyboard_press("Control+c")

        snap = await backend.raw_snapshot(1)
        snap_json = json.dumps(snap)
        _assert_no_card_leak(snap_json)
        assert CARDHOLDER not in snap_json

        _assert_no_card_leak(result.get_text())
        _assert_no_card_leak(json.dumps(data))
        _assert_no_card_leak(caplog.text)
    finally:
        await close_browser_backend(ctx)
