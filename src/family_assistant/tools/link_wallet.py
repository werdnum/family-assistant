"""Stripe Link Agent Wallet tools (Milestone 1).

Provides per-user wallet inspection, saved shipping addresses, and spend-request
lifecycle tools bound strictly to ``exec_context.user_id``:

- ``link_wallet_status`` — currency, spend limits, verification status, and
  redacted payment-method summaries (never shipping addresses or full card
  numbers).
- ``link_list_shipping_addresses`` — saved shipping addresses (tagged
  ``sensitive_data`` so digital checkouts never read home addresses).
- ``link_create_spend_request`` — creates a ``card`` spend request with
  mechanical HTTPS ``merchant_url`` origin binding against any active browser
  session, brief approval polling, and zero card credential fields.
- ``link_check_spend_request`` — inspects status, ``next_action``, and
  ``payment_status_details`` (no credentials).
- ``link_cancel_spend_request`` — cancels an unused spend request.

Every Link tool latches the active browser scope into ``checked`` review mode.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from family_assistant.services.link_provider import LinkScope
from family_assistant.services.link_wallet import LinkWalletError, LinkWalletService
from family_assistant.services.oauth_credentials import OAuthCredentialError
from family_assistant.tools.browser_autofill import _AUTOFILL_WAIT_SECONDS
from family_assistant.tools.browser_backend import get_existing_browser_backend
from family_assistant.tools.browser_session import (
    browser_scope_key,
    mark_browser_scope_checked,
)
from family_assistant.tools.types import ToolDefinition, ToolResult
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from link.models import (
        PaymentMethod,
        ShippingAddressRecord,
        SpendRequest,
        UserInfo,
    )
    from link.params import (
        LineItemParams,
        SpendRequestRecurringParams,
        TotalParams,
    )

    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# ast-grep-ignore: no-dict-any - Tool argument payloads from LLM JSON schema.
type LinkToolJson = dict[str, Any]

_MIN_CONTEXT_LENGTH = 100
_SPEND_REQUEST_WAIT_SECONDS = float(_AUTOFILL_WAIT_SECONDS)
_SPEND_REQUEST_POLL_INTERVAL_SECONDS = 2.0

LINK_TOOL_REQUIRED_SCOPES: dict[str, frozenset[str]] = {
    "link_wallet_status": frozenset({LinkScope.PAYMENT_METHODS_AGENTIC.value}),
    "link_list_shipping_addresses": frozenset({
        LinkScope.PAYMENT_METHODS_AGENTIC.value
    }),
    "link_create_spend_request": frozenset({LinkScope.PAYMENT_METHODS_AGENTIC.value}),
    "link_check_spend_request": frozenset({LinkScope.PAYMENT_METHODS_AGENTIC.value}),
    "link_cancel_spend_request": frozenset({LinkScope.PAYMENT_METHODS_AGENTIC.value}),
}


LINK_WALLET_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "link_wallet_status",
            "description": (
                "Inspect the user's connected Stripe Link Agent Wallet: default currency, "
                "spend limits, verification requirement, and redacted summaries of saved "
                "payment methods (brand, last 4 digits, default status, and whether each "
                "card is eligible for agentic payments). Does not return shipping addresses "
                "or full card numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_list_shipping_addresses",
            "description": (
                "List the user's saved shipping addresses from their connected Stripe Link "
                "wallet. Only call this when a physical delivery requires a shipping address."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_create_spend_request",
            "description": (
                "Create a Stripe Link virtual-card spend request for the user to approve "
                "out-of-band in Link. Requires an integer amount in minor currency units "
                "(e.g. cents), 3-letter currency code, merchant name, exact HTTPS merchant "
                "URL (which must match the active browser page origin when browsing), and "
                "a detailed context of at least 100 characters explaining the purchase. "
                "Returns status and approval URL or next action — never card credentials."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "integer",
                        "description": (
                            "Total purchase amount in minor currency units (positive integer, "
                            "e.g. 2599 for $25.99)."
                        ),
                        "minimum": 1,
                    },
                    "currency": {
                        "type": "string",
                        "description": "Three-letter ISO currency code (e.g. 'usd', 'aud').",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Detailed explanation of the purchase shown to the user and Link "
                            "(minimum 100 characters): items, quantities, price breakdown, "
                            "merchant, and why the user asked for it."
                        ),
                        "minLength": 100,
                    },
                    "merchant_name": {
                        "type": "string",
                        "description": "Human-readable name of the merchant.",
                    },
                    "merchant_url": {
                        "type": "string",
                        "description": (
                            "Exact HTTPS URL of the merchant checkout page. When a browser "
                            "session is open, its HTTPS origin must match the active page."
                        ),
                    },
                    "payment_details": {
                        "type": "string",
                        "description": (
                            "Optional payment method ID from link_wallet_status (must be an "
                            "eligible card). Omit to use the user's default eligible card."
                        ),
                    },
                    "line_items": {
                        "type": "array",
                        "description": "Optional itemized line items for the order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "quantity": {"type": "integer"},
                                "unit_amount": {"type": "integer"},
                                "description": {"type": "string"},
                                "sku": {"type": "string"},
                                "url": {"type": "string"},
                                "product_url": {"type": "string"},
                                "image_url": {"type": "string"},
                            },
                            "required": ["name"],
                        },
                    },
                    "totals": {
                        "type": "array",
                        "description": (
                            "Optional order total breakdown entries (each with type, "
                            "display_text, and integer minor-unit amount)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "display_text": {"type": "string"},
                                "amount": {"type": "integer"},
                            },
                            "required": ["type", "display_text", "amount"],
                        },
                    },
                    "expires_at": {
                        "type": ["integer", "string"],
                        "description": (
                            "Optional expiry as Unix seconds or UTC ISO-8601 timestamp "
                            "ending in 'Z'."
                        ),
                    },
                    "recurring": {
                        "type": "object",
                        "description": (
                            "Optional recurring schedule ({interval: 'hour'|'day'|'week'|"
                            "'month'|'year', interval_count?: integer})."
                        ),
                        "properties": {
                            "interval": {
                                "type": "string",
                                "enum": ["hour", "day", "week", "month", "year"],
                            },
                            "interval_count": {"type": "integer", "minimum": 1},
                        },
                        "required": ["interval"],
                    },
                },
                "required": [
                    "amount",
                    "currency",
                    "context",
                    "merchant_name",
                    "merchant_url",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_check_spend_request",
            "description": (
                "Check the current status, approval URL, next_action, or payment outcome "
                "of an existing Stripe Link spend request. Never returns card credentials."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spend_request_id": {
                        "type": "string",
                        "description": "The Link spend request ID to inspect.",
                    },
                },
                "required": ["spend_request_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_cancel_spend_request",
            "description": "Cancel an unused Stripe Link spend request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "spend_request_id": {
                        "type": "string",
                        "description": "The Link spend request ID to cancel.",
                    },
                },
                "required": ["spend_request_id"],
            },
        },
    },
]


def normalize_https_origin(url: str) -> str | None:
    """Return canonical ``https://host[:port]`` origin, or None if non-HTTPS/malformed."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == 443:
        return f"https://{host}"
    return f"https://{host}:{port}"


async def _wait_for_poll(seconds: float) -> None:
    """Sleep between spend-request approval polls (monkeypatchable in tests)."""
    await asyncio.sleep(seconds)


async def _latch_browser_scope_checked(exec_context: ToolExecutionContext) -> None:
    """Latch the active browser scope into ``checked`` mode."""
    scope = await browser_scope_key(exec_context)
    mark_browser_scope_checked(scope)


async def _guard(
    impl: Callable[[], Awaitable[ToolResult]],
) -> ToolResult:
    """Run a Link tool body, rendering credential/API failures as tool errors."""
    try:
        return await impl()
    except (OAuthCredentialError, LinkWalletError) as exc:
        return ToolResult(text=f"Error: {exc}", data={"error": str(exc)})


def _summarize_payment_method(pm: PaymentMethod) -> LinkToolJson:
    """Return a redacted payment-method summary (never full card digits or addresses)."""
    capabilities = pm.capabilities or {}
    agentic = capabilities.get("agentic_payments")
    eligible = agentic.eligible if agentic is not None else True
    ineligibility_reasons = (
        list(agentic.ineligibility_reasons) if agentic is not None else []
    )
    brand: str | None = None
    last4: str | None = None
    if pm.card_details is not None:
        brand = pm.card_details.brand
        last4 = pm.card_details.last4
    elif pm.bank_account_details is not None:
        last4 = pm.bank_account_details.last4

    return {
        "id": pm.id,
        "brand": brand,
        "last4": last4,
        "is_default": pm.is_default,
        "eligible": eligible,
        "ineligibility_reasons": ineligibility_reasons,
    }


def _format_user_info_section(user_info: UserInfo | None) -> LinkToolJson:
    """Extract only currency, spend limits, and verification status from UserInfo."""
    if user_info is None:
        return {
            "default_currency": None,
            "spend_limits": None,
            "verification_requirement": None,
        }
    spend_limits: LinkToolJson | None = None
    if user_info.agent_wallet_spend_limits is not None:
        limits = user_info.agent_wallet_spend_limits
        spend_limits = {
            "per_transaction": {"limit": limits.per_transaction.limit},
            "daily": {
                "limit": limits.daily.limit,
                "used": limits.daily.used,
                "remaining": limits.daily.remaining,
            },
            "thirty_day": {
                "limit": limits.thirty_day.limit,
                "used": limits.thirty_day.used,
                "remaining": limits.thirty_day.remaining,
            },
        }
    verification: LinkToolJson | None = None
    if user_info.agent_wallet_verification_requirement is not None:
        req = user_info.agent_wallet_verification_requirement
        verification = {
            "status": str(req.status),
            "action_url": req.action_url,
        }
    return {
        "default_currency": user_info.default_spend_request_currency,
        "spend_limits": spend_limits,
        "verification_requirement": verification,
    }


def _format_shipping_address(record: ShippingAddressRecord) -> LinkToolJson:
    """Serialize one saved shipping address record."""
    return {
        "id": record.id,
        "is_default": record.is_default,
        "nickname": record.nickname,
        "address": (
            record.address.model_dump(exclude_none=True)
            if record.address is not None
            else None
        ),
    }


def _format_spend_request_result(sr: SpendRequest) -> ToolResult:
    """Format a SpendRequest as metadata-only ToolResult (never card credentials)."""
    data: LinkToolJson = {
        "spend_request_id": sr.id,
        "status": str(sr.status),
        "amount": sr.amount,
        "currency": sr.currency,
        "merchant_name": sr.merchant_name,
        "merchant_url": sr.merchant_url,
        "card_brand": sr.card_brand,
        "card_last4": sr.card_last4,
        "created_at": sr.created_at,
        "updated_at": sr.updated_at,
    }
    if sr.approval_url is not None:
        data["approval_url"] = sr.approval_url
    if sr.expires_at is not None:
        data["expires_at"] = sr.expires_at

    next_action_info: LinkToolJson | None = None
    if sr.status_details is not None and sr.status_details.requires_action is not None:
        req_action = sr.status_details.requires_action
        na = req_action.next_action
        next_action_info = {
            "type": str(na.type),
            "resolution": str(na.resolution),
            "display_message": na.display_message,
            "action_url": na.action_url,
            "expires_at": na.expires_at,
        }
        data["failure_code"] = req_action.failure_code
        data["next_action"] = next_action_info
        if sr.status == "requires_action":
            data["resolution"] = str(na.resolution)
            data["display_message"] = na.display_message
            data["action_url"] = na.action_url

    if sr.payment_status_details is not None:
        psd = sr.payment_status_details
        data["payment_status_details"] = {
            "outcome": str(psd.outcome),
            "code": psd.code,
            "decline_code": psd.decline_code,
            "amount": psd.amount,
            "currency": psd.currency,
            "created": psd.created,
        }

    status_str = str(sr.status)
    amount_label = (
        f"{sr.amount} {sr.currency.upper()}"
        if sr.amount is not None and sr.currency
        else "unknown amount"
    )
    merchant_label = sr.merchant_name or sr.merchant_url or "the merchant"

    if status_str == "approved":
        card_suffix = (
            f" ({sr.card_brand} ending in {sr.card_last4})"
            if sr.card_brand and sr.card_last4
            else ""
        )
        text = (
            f"Link spend request {sr.id} for {amount_label} at {merchant_label} "
            f"is approved{card_suffix}."
        )
    elif status_str == "pending_approval":
        url_part = f" Approval URL: {sr.approval_url}" if sr.approval_url else ""
        text = (
            f"Link spend request {sr.id} for {amount_label} at {merchant_label} "
            f"is pending user approval.{url_part} Share the approval URL with the user "
            "and stop here until they approve it and ask you to continue."
        )
    elif status_str == "requires_action" and next_action_info is not None:
        resolution = next_action_info["resolution"]
        display_message = next_action_info["display_message"]
        action_url = next_action_info["action_url"]
        url_part = f" Action URL: {action_url}" if action_url else ""
        if resolution == "auto_resume":
            guidance = (
                "Once the user completes this step, re-check this spend request with "
                "link_check_spend_request."
            )
        else:
            guidance = (
                "After the user completes this step, create a new spend request "
                "(do not reuse this spend_request_id)."
            )
        text = (
            f"Link spend request {sr.id} requires user action "
            f"({next_action_info['type']}, resolution={resolution}): "
            f"{display_message}.{url_part} {guidance}"
        )
    elif status_str in {"denied", "canceled", "expired"}:
        text = (
            f"Link spend request {sr.id} for {amount_label} at {merchant_label} "
            f"is {status_str}. Do not retry automatically without user direction."
        )
    else:
        text = (
            f"Link spend request {sr.id} for {amount_label} at {merchant_label} "
            f"has status '{status_str}'."
        )

    return ToolResult(text=text, data=data)


async def link_wallet_status_tool(exec_context: ToolExecutionContext) -> ToolResult:
    """Return Link wallet currency, limits, verification status, and redacted cards."""
    await _latch_browser_scope_checked(exec_context)

    async def _run() -> ToolResult:
        service = LinkWalletService.for_context(exec_context)
        payment_methods = await service.list_payment_methods()
        user_info: UserInfo | None = None
        if LinkScope.USERINFO_READ.value in service.configured_scopes:
            user_info = await service.get_user_info()

        cards = [_summarize_payment_method(pm) for pm in payment_methods]
        info_data = _format_user_info_section(user_info)
        data: LinkToolJson = {
            **info_data,
            "payment_methods": cards,
        }

        eligible_count = sum(1 for card in cards if card["eligible"])
        ineligible_count = len(cards) - eligible_count
        currency = info_data["default_currency"] or "unknown"
        verification_status = (
            info_data["verification_requirement"]["status"]
            if isinstance(info_data["verification_requirement"], dict)
            else "unknown"
        )
        lines = [
            f"Link wallet status: default currency={currency}, "
            f"verification={verification_status}, "
            f"payment methods={len(cards)} ({eligible_count} eligible, "
            f"{ineligible_count} ineligible).",
        ]
        for card in cards:
            brand = card["brand"] or "card"
            last4 = card["last4"] or "????"
            default_tag = " [default]" if card["is_default"] else ""
            if card["eligible"]:
                elig_tag = "eligible"
            else:
                reasons = ", ".join(card["ineligibility_reasons"]) or "restricted"
                elig_tag = f"ineligible ({reasons})"
            lines.append(
                f"- {card['id']}: {brand} ****{last4}{default_tag} — {elig_tag}"
            )
        return ToolResult(text="\n".join(lines), data=data)

    return await _guard(_run)


async def link_list_shipping_addresses_tool(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Return saved shipping addresses from the user's Link wallet."""
    await _latch_browser_scope_checked(exec_context)

    async def _run() -> ToolResult:
        service = LinkWalletService.for_context(exec_context)
        records = await service.list_shipping_addresses()
        addresses = [_format_shipping_address(record) for record in records]
        if not addresses:
            # ast-grep-ignore: toolresult-text-literal-with-data - Empty-list summary conveys full result
            return ToolResult(
                text="No saved shipping addresses found in the user's Link wallet.",
                data={"shipping_addresses": []},
            )
        lines = [f"Found {len(addresses)} saved Link shipping address(es):"]
        for entry in addresses:
            label = entry["nickname"] or entry["id"]
            default_tag = " [default]" if entry["is_default"] else ""
            addr = entry["address"] or {}
            parts = [
                addr.get(k)
                for k in (
                    "name",
                    "line_1",
                    "line_2",
                    "locality",
                    "administrative_area",
                    "postal_code",
                    "country_code",
                )
                if addr.get(k)
            ]
            summary = ", ".join(str(part) for part in parts) or "(empty address)"
            lines.append(f"- {label}{default_tag}: {summary}")
        return ToolResult(
            text="\n".join(lines),
            data={"shipping_addresses": addresses},
        )

    return await _guard(_run)


def _error_result(code: str, message: str) -> ToolResult:
    """Return a structured validation error ToolResult."""
    return ToolResult(text=f"Error: {message}", data={"error": code})


async def link_create_spend_request_tool(
    exec_context: ToolExecutionContext,
    amount: int,
    currency: str,
    context: str,
    merchant_name: str,
    merchant_url: str,
    payment_details: str | None = None,
    line_items: list[LinkToolJson] | None = None,
    totals: list[LinkToolJson] | None = None,
    expires_at: int | str | None = None,
    recurring: LinkToolJson | None = None,
) -> ToolResult:
    """Create a Link virtual-card spend request with origin binding and brief polling."""
    await _latch_browser_scope_checked(exec_context)

    if isinstance(amount, bool) or amount <= 0:
        return _error_result(
            "invalid_amount",
            "amount must be a positive integer in minor currency units.",
        )
    normalized_currency = currency.strip().lower()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        return _error_result(
            "invalid_currency",
            "currency must be a 3-letter ISO currency code (e.g. 'usd', 'aud').",
        )
    cleaned_context = context.strip()
    if len(cleaned_context) < _MIN_CONTEXT_LENGTH:
        return _error_result(
            "context_too_short",
            f"context must be at least {_MIN_CONTEXT_LENGTH} characters "
            f"(got {len(cleaned_context)}). Describe the items, prices, merchant, "
            "and user request in detail.",
        )
    cleaned_merchant_name = merchant_name.strip()
    if not cleaned_merchant_name:
        return _error_result(
            "invalid_merchant_name", "merchant_name must not be empty."
        )
    merchant_origin = normalize_https_origin(merchant_url)
    if merchant_origin is None:
        return _error_result(
            "invalid_merchant_url",
            "merchant_url must be a valid HTTPS URL (plaintext HTTP or "
            "malformed URLs are refused).",
        )

    # Mechanical merchant_url origin binding: when a browser session is active,
    # require the active page to be on the exact same HTTPS origin so a hostile
    # page cannot steer a spend request to a mismatched merchant_url.
    active_backend = await get_existing_browser_backend(exec_context)
    if active_backend is not None:
        browser_url = active_backend.current_url
        browser_origin = normalize_https_origin(browser_url)
        if browser_origin is None:
            return ToolResult(
                text=(
                    "Error: Refusing to create a Link spend request because the active "
                    f"browser page ({browser_url!r}) is not an HTTPS origin."
                ),
                data={
                    "error": "browser_not_https",
                    "browser_url": browser_url,
                    "merchant_origin": merchant_origin,
                },
            )
        if browser_origin != merchant_origin:
            return ToolResult(
                text=(
                    f"Error: merchant_url origin ({merchant_origin}) does not match the "
                    f"active browser page origin ({browser_origin}). Navigate to the "
                    "matching merchant checkout page or correct merchant_url before "
                    "creating a spend request."
                ),
                data={
                    "error": "merchant_url_origin_mismatch",
                    "merchant_origin": merchant_origin,
                    "browser_origin": browser_origin,
                },
            )

    async def _run() -> ToolResult:
        service = LinkWalletService.for_context(exec_context)
        sr = await service.create_spend_request(
            amount=amount,
            currency=normalized_currency,
            context=cleaned_context,
            merchant_name=cleaned_merchant_name,
            merchant_url=merchant_url.strip(),
            payment_details=payment_details,
            line_items=cast("list[LineItemParams] | None", line_items),
            totals=cast("list[TotalParams] | None", totals),
            expires_at=expires_at,
            recurring=cast("SpendRequestRecurringParams | None", recurring),
        )
        if sr.status == "pending_approval" and _SPEND_REQUEST_WAIT_SECONDS > 0:
            clock = exec_context.clock or SystemClock()
            deadline = clock.now().timestamp() + _SPEND_REQUEST_WAIT_SECONDS
            while sr.status == "pending_approval":
                remaining = deadline - clock.now().timestamp()
                if remaining <= 0:
                    break
                step = min(_SPEND_REQUEST_POLL_INTERVAL_SECONDS, remaining)
                await _wait_for_poll(step)
                refreshed = await service.retrieve_spend_request(sr.id)
                if refreshed is None:
                    break
                sr = refreshed
        return _format_spend_request_result(sr)

    return await _guard(_run)


async def link_check_spend_request_tool(
    exec_context: ToolExecutionContext,
    spend_request_id: str,
) -> ToolResult:
    """Inspect a Link spend request's status, next_action, and payment outcome."""
    await _latch_browser_scope_checked(exec_context)
    cleaned_id = spend_request_id.strip()
    if not cleaned_id:
        return _error_result(
            "invalid_spend_request_id", "spend_request_id must not be empty."
        )

    async def _run() -> ToolResult:
        service = LinkWalletService.for_context(exec_context)
        sr = await service.retrieve_spend_request(cleaned_id)
        if sr is None:
            return ToolResult(
                text=f"Error: Link spend request {cleaned_id!r} was not found.",
                data={"error": "not_found", "spend_request_id": cleaned_id},
            )
        return _format_spend_request_result(sr)

    return await _guard(_run)


async def link_cancel_spend_request_tool(
    exec_context: ToolExecutionContext,
    spend_request_id: str,
) -> ToolResult:
    """Cancel an unused Link spend request."""
    await _latch_browser_scope_checked(exec_context)
    cleaned_id = spend_request_id.strip()
    if not cleaned_id:
        return _error_result(
            "invalid_spend_request_id", "spend_request_id must not be empty."
        )

    async def _run() -> ToolResult:
        service = LinkWalletService.for_context(exec_context)
        sr = await service.cancel_spend_request(cleaned_id)
        return _format_spend_request_result(sr)

    return await _guard(_run)


__all__ = [
    "LINK_TOOL_REQUIRED_SCOPES",
    "LINK_WALLET_TOOLS_DEFINITION",
    "link_cancel_spend_request_tool",
    "link_check_spend_request_tool",
    "link_create_spend_request_tool",
    "link_list_shipping_addresses_tool",
    "link_wallet_status_tool",
    "normalize_https_origin",
]
