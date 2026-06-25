"""Shopping tools backed by UCP merchant MCP endpoints.

These tools work with any merchant that advertises a Universal Commerce Protocol
(UCP) profile at ``/.well-known/ucp``. The merchant's shopping MCP endpoint is
discovered from that profile; when a merchant does not advertise one, the tools
fall back to the Shopify convention (``/api/ucp/mcp``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from family_assistant.services.ucp import (
    UCPConfigurationError,
    discover_merchant_ucp_profile,
    has_ucp_signing_key,
    load_ucp_signing_key,
    merchant_origin,
    sign_ucp_request,
    ucp_agent_header,
    ucp_profile_url,
)
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.services.ucp import MerchantUCPProfile
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


SHOPIFY_FALLBACK_MCP_PATH = "/api/ucp/mcp"
# Bound the discovery GET so a store without a UCP profile (which may tarpit the
# well-known path) falls back to the Shopify endpoint promptly instead of paying
# the full MCP POST timeout before each call.
UCP_DISCOVERY_TIMEOUT_SECONDS = 5.0
CART_CAPABILITY = "dev.ucp.shopping.cart"
CHECKOUT_CAPABILITY = "dev.ucp.shopping.checkout"


@dataclass(frozen=True, slots=True)
class _ResolvedMerchant:
    """The endpoint to use for a merchant plus its cart/checkout shape.

    ``checkout_only`` is True when the merchant advertises the checkout
    capability but not the cart capability, meaning its MCP endpoint has no
    cart methods and a cart must be skipped in favour of a direct checkout.
    """

    endpoint: str
    checkout_only: bool


SHOPPING_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "ucp_add_to_cart",
            "description": (
                "Create or update a UCP merchant cart with selected product variants. "
                "Works with any merchant that supports the Universal Commerce Protocol "
                "(UCP). Use after the buyer selects specific variants and quantities. "
                "For checkout-only merchants (no cart support) this instead opens a "
                "checkout session directly from the line items and returns its "
                "handoff link; in that case omit cart_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "business_url": {
                        "type": "string",
                        "description": (
                            "Merchant storefront origin, for example "
                            "https://shop.example.com."
                        ),
                    },
                    "line_items": {
                        "type": "array",
                        "description": (
                            "Items to add. Each item needs quantity and either "
                            "variant_id or item.id."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "variant_id": {
                                    "type": "string",
                                    "description": (
                                        "Merchant product variant ID (a Shopify "
                                        "ProductVariant GID for Shopify stores)."
                                    ),
                                },
                                "quantity": {
                                    "type": "integer",
                                    "minimum": 1,
                                },
                            },
                            "required": ["quantity"],
                        },
                    },
                    "cart_id": {
                        "type": "string",
                        "description": (
                            "Existing cart ID to update. Omit to create a new cart."
                        ),
                    },
                    "context": {
                        "type": "object",
                        "description": (
                            "Optional localization hints such as address_country, "
                            "address_region, or postal_code."
                        ),
                    },
                },
                "required": ["business_url", "line_items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ucp_get_cart",
            "description": "Fetch the current UCP cart state before editing or handoff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "business_url": {
                        "type": "string",
                        "description": "Merchant storefront origin.",
                    },
                    "cart_id": {
                        "type": "string",
                        "description": "UCP cart ID.",
                    },
                },
                "required": ["business_url", "cart_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ucp_transfer_checkout_to_human",
            "description": (
                "Create a UCP checkout session from a cart and return the merchant "
                "checkout URL for the buyer to complete payment themselves. "
                "This never completes checkout autonomously."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "business_url": {
                        "type": "string",
                        "description": "Merchant storefront origin.",
                    },
                    "cart_id": {
                        "type": "string",
                        "description": "UCP cart ID to convert to checkout.",
                    },
                },
                "required": ["business_url", "cart_id"],
            },
        },
    },
]


def _get_app_config(exec_context: ToolExecutionContext) -> AppConfig:
    if (
        exec_context.processing_service is not None
        and exec_context.processing_service.app_config is not None
    ):
        return exec_context.processing_service.app_config
    msg = "UCP shopping tools require application configuration."
    raise ValueError(msg)


def _is_checkout_only(profile: MerchantUCPProfile | None) -> bool:
    """Whether the merchant advertises checkout but not cart capabilities.

    Such "checkout-only" merchants (e.g. Adore Beauty) expose no cart methods on
    their MCP endpoint, so ``create_cart``/``update_cart`` would fail (403); a
    checkout session must be created directly from the line items instead. A
    merchant with no profile (Shopify fallback) or one that advertises no
    capabilities at all is treated as cart-capable, preserving the cart flow.
    """
    if profile is None:
        return False
    capabilities = set(profile.capability_names)
    return CHECKOUT_CAPABILITY in capabilities and CART_CAPABILITY not in capabilities


async def _resolve_ucp_endpoint(
    business_url: str, *, client: httpx.AsyncClient
) -> _ResolvedMerchant:
    """Resolve the merchant's shopping MCP endpoint for ``business_url``.

    Discovers the endpoints from the merchant's ``/.well-known/ucp`` profile and
    falls back to the Shopify convention (``/api/ucp/mcp``) when the merchant
    advertises no usable shopping MCP binding. The returned ``checkout_only``
    flag reflects the discovered capabilities so callers can bypass the cart for
    checkout-only merchants.

    Only a binding that is same-origin as ``business_url`` is accepted, and every
    advertised binding is considered (not just the first) so a usable same-origin
    binding is still selected when a cross-origin one is listed ahead of it. The
    profile is untrusted merchant-controlled metadata, so a cross-origin (or
    protocol-relative) endpoint could otherwise redirect the signed POST at an
    arbitrary/internal host — an SSRF vector. Discovery is also given a short
    timeout so a store that does not publish a profile (and may tarpit the
    well-known path) falls back promptly instead of stalling the POST.
    """
    origin = merchant_origin(business_url)
    if origin is None:
        msg = "business_url must be an https merchant origin."
        raise ValueError(msg)
    profile = await discover_merchant_ucp_profile(
        business_url, client=client, timeout=UCP_DISCOVERY_TIMEOUT_SECONDS
    )
    checkout_only = _is_checkout_only(profile)
    if profile is not None:
        same_origin_endpoint = profile.same_origin_mcp_endpoint
        if same_origin_endpoint is not None:
            return _ResolvedMerchant(
                endpoint=same_origin_endpoint, checkout_only=checkout_only
            )
        if profile.mcp_endpoints:
            logger.warning(
                "Ignoring %d cross-origin UCP endpoint(s) advertised by %s",
                len(profile.mcp_endpoints),
                origin,
            )
    return _ResolvedMerchant(
        endpoint=f"{origin}{SHOPIFY_FALLBACK_MCP_PATH}", checkout_only=checkout_only
    )


async def _resolve_endpoint_for(business_url: str) -> _ResolvedMerchant:
    """Resolve the merchant MCP endpoint once, with a dedicated short client.

    Resolving up front (rather than inside each POST) means a multi-step
    operation — such as a cart update that reads then writes — uses one
    consistent endpoint instead of rediscovering per call, where a flaky second
    discovery could split reads and writes across different endpoints.
    """
    async with httpx.AsyncClient(timeout=UCP_DISCOVERY_TIMEOUT_SECONDS) as client:
        return await _resolve_ucp_endpoint(business_url, client=client)


def _normalize_line_items(
    line_items: list[dict[str, object]],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for line_item in line_items:
        quantity = line_item.get("quantity")
        if not isinstance(quantity, int) or quantity < 1:
            msg = "Each line item quantity must be a positive integer."
            raise ValueError(msg)

        item = line_item.get("item")
        item_id = line_item.get("variant_id") or line_item.get("id")
        if isinstance(item, dict):
            item_id = item.get("id") or item_id
        if not isinstance(item_id, str) or not item_id:
            msg = "Each line item must include variant_id or item.id."
            raise ValueError(msg)

        normalized.append({"quantity": quantity, "item": {"id": item_id}})
    if not normalized:
        msg = "At least one line item is required."
        raise ValueError(msg)
    return normalized


def _json_rpc_body(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": str(uuid4()),
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }


def _with_ucp_meta(
    app_config: AppConfig, arguments: dict[str, object]
) -> dict[str, object]:
    return {
        "meta": {"ucp-agent": {"profile": ucp_profile_url(app_config)}},
        **arguments,
    }


def _json_bytes(body: object) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _json_rpc_error_text(error: dict[object, object]) -> str:
    message = error.get("message")
    data = error.get("data")
    if isinstance(data, dict):
        content = data.get("content")
        code = data.get("code")
        if isinstance(content, str) and content:
            if isinstance(code, str) and code:
                return f"{content} ({code})"
            return content
    if isinstance(message, str) and message:
        return message
    return "UCP request failed."


def _raise_for_ucp_failure(
    response: httpx.Response, response_data: dict[object, object]
) -> None:
    error = response_data.get("error")
    if isinstance(error, dict):
        msg = f"UCP JSON-RPC error: {_json_rpc_error_text(error)}"
        raise ValueError(msg)

    if response.is_error:
        msg = f"UCP HTTP error {response.status_code}."
        raise ValueError(msg)


def _require_signing_config(app_config: AppConfig) -> None:
    """Raise a clear error when UCP signing is not usable.

    Validates both presence and that the key material actually loads (not
    malformed/encrypted/wrong-type), so a misconfigured deployment fails fast
    before any merchant network work that cannot succeed.
    """
    if not has_ucp_signing_key(app_config):
        msg = (
            "UCP checkout handoff requires UCP signing configuration. "
            "Set UCP_SIGNING_KEY_ID and UCP_SIGNING_PRIVATE_KEY or "
            "UCP_SIGNING_PRIVATE_KEY_PATH."
        )
        raise ValueError(msg)
    try:
        load_ucp_signing_key(app_config)
    except UCPConfigurationError as exc:
        raise ValueError(str(exc)) from exc


async def _post_ucp_tool_call(
    app_config: AppConfig,
    *,
    endpoint: str,
    tool_name: str,
    arguments: dict[str, object],
    require_signed: bool = False,
) -> dict[str, object]:
    body = _json_rpc_body(tool_name, _with_ucp_meta(app_config, arguments))

    if require_signed:
        _require_signing_config(app_config)

    if has_ucp_signing_key(app_config):
        try:
            signed_request = sign_ucp_request(
                app_config,
                method="POST",
                url=endpoint,
                body=body,
            )
        except UCPConfigurationError as exc:
            raise ValueError(str(exc)) from exc
        request_headers = signed_request.headers
        request_body = signed_request.body
    else:
        request_headers = {
            "Content-Type": "application/json",
            "UCP-Agent": ucp_agent_header(app_config),
        }
        request_body = _json_bytes(body)

    logger.info("Calling UCP tool %s at %s", tool_name, endpoint)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            endpoint, headers=request_headers, content=request_body
        )

    try:
        response_data = response.json()
    except json.JSONDecodeError as exc:
        msg = f"UCP response was not JSON: HTTP {response.status_code}"
        raise ValueError(msg) from exc

    if not isinstance(response_data, dict):
        msg = "UCP response JSON must be an object."
        raise ValueError(msg)
    _raise_for_ucp_failure(response, response_data)

    return {
        "status_code": response.status_code,
        "response": response_data,
        "request": {
            "url": endpoint,
            "tool_name": tool_name,
            "signed": has_ucp_signing_key(app_config),
        },
    }


def _structured_content(response_data: dict[str, object]) -> dict[str, object]:
    response = response_data.get("response")
    if not isinstance(response, dict):
        return {}
    result = response.get("result")
    if not isinstance(result, dict):
        return {}
    structured = result.get("structuredContent")
    return structured if isinstance(structured, dict) else {}


def _message_text(
    message: dict[object, object],
    *,
    default: str = "Merchant returned an unusable response.",
) -> str:
    content = message.get("content")
    code = message.get("code")
    if isinstance(content, str) and content:
        if isinstance(code, str) and code:
            return f"{content} ({code})"
        return content
    if isinstance(code, str) and code:
        return code
    return default


def _cart_failure_text(cart: dict[str, object]) -> str | None:
    ucp = cart.get("ucp")
    if isinstance(ucp, dict):
        status = ucp.get("status")
        if isinstance(status, str) and status.lower() in {"error", "failed"}:
            return f"Merchant returned cart status {status}."

    messages = cart.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
            severity = message.get("severity")
            if message_type == "error" or severity == "unrecoverable":
                return _message_text(
                    message,
                    default="Merchant returned an unusable cart response.",
                )
    return None


def _cart_from_response(response_data: dict[str, object]) -> dict[str, object]:
    structured = _structured_content(response_data)
    cart = structured.get("cart")
    if not isinstance(cart, dict):
        msg = "UCP response did not include a cart."
        raise ValueError(msg)
    failure_text = _cart_failure_text(cart)
    if failure_text is not None or not isinstance(cart.get("id"), str):
        msg = failure_text or "UCP cart response did not include a cart ID."
        raise ValueError(msg)
    return cart


def _checkout_from_response(response_data: dict[str, object]) -> dict[str, object]:
    return _structured_content(response_data)


def _checkout_failure_text(checkout: dict[str, object]) -> str | None:
    status = checkout.get("status")
    if isinstance(status, str) and status.lower() in {"canceled", "error", "failed"}:
        return f"Merchant returned checkout status {status}."

    messages = checkout.get("messages")
    if not isinstance(messages, list):
        return None

    checkout_has_id = isinstance(checkout.get("id"), str)
    for message in messages:
        if not isinstance(message, dict):
            continue
        code = message.get("code")
        severity = message.get("severity")
        message_type = message.get("type")
        if (
            code in {"invalid_cart_id", "cart_not_found"}
            or severity == "unrecoverable"
            or (message_type == "error" and not checkout_has_id)
        ):
            return _message_text(
                message,
                default="Merchant returned an unusable checkout response.",
            )
    return None


def _merge_cart_line_items(
    existing_cart: dict[str, object],
    new_line_items: list[dict[str, object]],
) -> list[dict[str, object]]:
    existing_items = existing_cart.get("line_items")
    merged: list[dict[str, object]] = []
    if isinstance(existing_items, list):
        for existing in existing_items:
            if isinstance(existing, dict):
                item = existing.get("item")
                quantity = existing.get("quantity")
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and isinstance(quantity, int)
                ):
                    merged.append({"quantity": quantity, "item": {"id": item["id"]}})

    for new_item in new_line_items:
        new_item_data = new_item.get("item")
        new_id = new_item_data.get("id") if isinstance(new_item_data, dict) else None
        new_quantity = new_item.get("quantity")
        if not isinstance(new_id, str) or not isinstance(new_quantity, int):
            msg = "Normalized line items must include item.id and integer quantity."
            raise ValueError(msg)
        merged_existing = False
        for existing in merged:
            existing_item = existing.get("item")
            existing_quantity = existing.get("quantity")
            if (
                isinstance(existing_item, dict)
                and existing_item.get("id") == new_id
                and isinstance(existing_quantity, int)
            ):
                existing["quantity"] = existing_quantity + new_quantity
                merged_existing = True
                break
        if not merged_existing:
            merged.append(new_item)
    return merged


def _cart_update_payload(
    existing_cart: dict[str, object],
    new_line_items: list[dict[str, object]],
    context: dict[str, object] | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "line_items": _merge_cart_line_items(existing_cart, new_line_items)
    }

    existing_context = existing_cart.get("context")
    if context is not None:
        payload["context"] = context
    elif isinstance(existing_context, dict):
        payload["context"] = existing_context

    for field in ("buyer", "signals"):
        existing_value = existing_cart.get(field)
        if isinstance(existing_value, dict):
            payload[field] = existing_value

    return payload


def _cart_result_text(cart: dict[str, object]) -> str:
    cart_id = cart.get("id")
    continue_url = cart.get("continue_url")
    if isinstance(cart_id, str) and isinstance(continue_url, str):
        return f"Cart ready: {continue_url}\nCart ID: {cart_id}"
    if isinstance(cart_id, str):
        return f"Cart ready.\nCart ID: {cart_id}"
    return "Cart request completed."


async def _create_checkout_session(
    app_config: AppConfig,
    *,
    endpoint: str,
    arguments: dict[str, object],
) -> ToolResult:
    """Post a signed ``create_checkout`` call and render the handoff result.

    Shared by the cart-based checkout handoff and the checkout-only add-to-cart
    path; ``arguments`` carries either a ``cart_id`` or a ``line_items`` list.
    """
    response_data = await _post_ucp_tool_call(
        app_config,
        endpoint=endpoint,
        tool_name="create_checkout",
        arguments=arguments,
        require_signed=True,
    )
    checkout = _checkout_from_response(response_data)
    continue_url = checkout.get("continue_url")
    checkout_id = checkout.get("id")
    status = checkout.get("status")
    failure_text = _checkout_failure_text(checkout)
    if failure_text is not None:
        raise ValueError(failure_text)
    if not isinstance(checkout_id, str) or not checkout_id:
        msg = "UCP checkout response did not include a checkout ID."
        raise ValueError(msg)
    if not isinstance(status, str) or not status:
        msg = "UCP checkout response did not include a checkout status."
        raise ValueError(msg)
    if not isinstance(continue_url, str) or not continue_url:
        msg = "UCP checkout response did not include a continue_url."
        raise ValueError(msg)

    details = [
        f"Checkout link: {continue_url}",
        f"Status: {status}",
        f"Checkout ID: {checkout_id}",
        "The buyer must complete payment on the merchant checkout page.",
    ]
    return ToolResult(text="\n".join(details), data=response_data)


async def _add_to_cart_checkout_only(
    app_config: AppConfig,
    *,
    endpoint: str,
    normalized_items: list[dict[str, object]],
    cart_id: str | None,
    context: dict[str, object] | None,
) -> ToolResult:
    """Create a checkout session directly from line items, bypassing the cart.

    A checkout-only merchant exposes no cart methods, so there is no cart to
    create or update; ``cart_id`` is therefore unusable and rejected with a
    clear message rather than sent to an endpoint that would 403/404.
    """
    if cart_id:
        msg = (
            "This merchant only supports checkout (no cart), so an existing "
            "cart_id cannot be updated. Omit cart_id to start a checkout from "
            "the line items."
        )
        raise ValueError(msg)
    checkout_arguments: dict[str, object] = {"line_items": normalized_items}
    if context is not None:
        checkout_arguments["context"] = context
    return await _create_checkout_session(
        app_config, endpoint=endpoint, arguments=checkout_arguments
    )


async def ucp_add_to_cart_tool(
    exec_context: ToolExecutionContext,
    business_url: str,
    line_items: list[dict[str, object]],
    cart_id: str | None = None,
    context: dict[str, object] | None = None,
) -> ToolResult:
    """Create or update a UCP merchant cart.

    For checkout-only merchants (those advertising the checkout capability but
    not the cart capability) there is no cart endpoint, so a checkout session is
    created directly from the line items and its handoff link is returned.
    """
    app_config = _get_app_config(exec_context)
    normalized_items = _normalize_line_items(line_items)
    resolved = await _resolve_endpoint_for(business_url)

    if resolved.checkout_only:
        return await _add_to_cart_checkout_only(
            app_config,
            endpoint=resolved.endpoint,
            normalized_items=normalized_items,
            cart_id=cart_id,
            context=context,
        )

    if cart_id:
        current = await _post_ucp_tool_call(
            app_config,
            endpoint=resolved.endpoint,
            tool_name="get_cart",
            arguments={"id": cart_id},
        )
        existing_cart = _cart_from_response(current)
        cart_payload = _cart_update_payload(existing_cart, normalized_items, context)
        response_data = await _post_ucp_tool_call(
            app_config,
            endpoint=resolved.endpoint,
            tool_name="update_cart",
            arguments={"id": cart_id, "cart": cart_payload},
        )
    else:
        cart_payload: dict[str, object] = {"line_items": normalized_items}
        if context is not None:
            cart_payload["context"] = context
        response_data = await _post_ucp_tool_call(
            app_config,
            endpoint=resolved.endpoint,
            tool_name="create_cart",
            arguments={"cart": cart_payload},
        )

    cart = _cart_from_response(response_data)
    return ToolResult(text=_cart_result_text(cart), data=response_data)


async def ucp_get_cart_tool(
    exec_context: ToolExecutionContext,
    business_url: str,
    cart_id: str,
) -> ToolResult:
    """Fetch a UCP merchant cart."""
    app_config = _get_app_config(exec_context)
    resolved = await _resolve_endpoint_for(business_url)
    response_data = await _post_ucp_tool_call(
        app_config,
        endpoint=resolved.endpoint,
        tool_name="get_cart",
        arguments={"id": cart_id},
    )
    cart = _cart_from_response(response_data)
    return ToolResult(text=_cart_result_text(cart), data=response_data)


async def ucp_transfer_checkout_to_human_tool(
    exec_context: ToolExecutionContext,
    business_url: str,
    cart_id: str,
) -> ToolResult:
    """Create a checkout session and return the human handoff URL."""
    app_config = _get_app_config(exec_context)
    # Validate signing config before contacting the merchant: in an unsigned
    # deployment checkout cannot succeed, so fail fast instead of paying a
    # discovery round-trip first.
    _require_signing_config(app_config)
    resolved = await _resolve_endpoint_for(business_url)
    return await _create_checkout_session(
        app_config, endpoint=resolved.endpoint, arguments={"cart_id": cart_id}
    )
