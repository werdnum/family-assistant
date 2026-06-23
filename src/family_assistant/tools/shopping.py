"""Shopping tools backed by Shopify's UCP MCP endpoints."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from family_assistant.services.ucp import (
    UCPConfigurationError,
    has_ucp_signing_key,
    sign_ucp_request,
    ucp_agent_header,
    ucp_profile_url,
)
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


SHOPIFY_MCP_PATH = "/api/ucp/mcp"


SHOPPING_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "shopify_add_to_cart",
            "description": (
                "Create or update a Shopify/UCP cart with selected product variants. "
                "Use after the buyer selects specific variants and quantities."
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
                                    "description": "Shopify ProductVariant GID.",
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
            "name": "shopify_get_cart",
            "description": "Fetch the current Shopify/UCP cart state before editing or handoff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "business_url": {
                        "type": "string",
                        "description": "Merchant storefront origin.",
                    },
                    "cart_id": {
                        "type": "string",
                        "description": "Shopify cart ID.",
                    },
                },
                "required": ["business_url", "cart_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shopify_transfer_checkout_to_human",
            "description": (
                "Create a Shopify/UCP checkout session from a cart and return the "
                "merchant checkout URL for the buyer to complete payment themselves. "
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
                        "description": "Shopify cart ID to convert to checkout.",
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
    msg = "Shopify shopping tools require application configuration."
    raise ValueError(msg)


def _shopify_ucp_endpoint(business_url: str) -> str:
    parsed = urlparse(business_url)
    if parsed.scheme != "https" or not parsed.netloc:
        msg = "business_url must be an https merchant origin."
        raise ValueError(msg)
    return f"https://{parsed.netloc}{SHOPIFY_MCP_PATH}"


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
    return "Shopify UCP request failed."


def _raise_for_shopify_failure(
    response: httpx.Response, response_data: dict[object, object]
) -> None:
    error = response_data.get("error")
    if isinstance(error, dict):
        msg = f"Shopify UCP JSON-RPC error: {_json_rpc_error_text(error)}"
        raise ValueError(msg)

    if response.is_error:
        msg = f"Shopify UCP HTTP error {response.status_code}."
        raise ValueError(msg)


async def _post_shopify_tool_call(
    app_config: AppConfig,
    *,
    business_url: str,
    tool_name: str,
    arguments: dict[str, object],
    require_signed: bool = False,
) -> dict[str, object]:
    endpoint = _shopify_ucp_endpoint(business_url)
    body = _json_rpc_body(tool_name, _with_ucp_meta(app_config, arguments))

    if require_signed and not has_ucp_signing_key(app_config):
        msg = (
            "Shopify checkout handoff requires UCP signing configuration. "
            "Set UCP_SIGNING_KEY_ID and UCP_SIGNING_PRIVATE_KEY or "
            "UCP_SIGNING_PRIVATE_KEY_PATH."
        )
        raise ValueError(msg)

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

    logger.info("Calling Shopify UCP tool %s for %s", tool_name, business_url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            endpoint, headers=request_headers, content=request_body
        )

    try:
        response_data = response.json()
    except json.JSONDecodeError as exc:
        msg = f"Shopify UCP response was not JSON: HTTP {response.status_code}"
        raise ValueError(msg) from exc

    if not isinstance(response_data, dict):
        msg = "Shopify UCP response JSON must be an object."
        raise ValueError(msg)
    _raise_for_shopify_failure(response, response_data)

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


def _message_text(message: dict[object, object]) -> str:
    content = message.get("content")
    code = message.get("code")
    if isinstance(content, str) and content:
        if isinstance(code, str) and code:
            return f"{content} ({code})"
        return content
    if isinstance(code, str) and code:
        return code
    return "Merchant returned an unusable cart response."


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
                return _message_text(message)
    return None


def _cart_from_response(response_data: dict[str, object]) -> dict[str, object]:
    structured = _structured_content(response_data)
    cart = structured.get("cart")
    if not isinstance(cart, dict):
        msg = "Shopify UCP response did not include a cart."
        raise ValueError(msg)
    failure_text = _cart_failure_text(cart)
    if failure_text is not None or not isinstance(cart.get("id"), str):
        msg = failure_text or "Shopify cart response did not include a cart ID."
        raise ValueError(msg)
    return cart


def _checkout_from_response(response_data: dict[str, object]) -> dict[str, object]:
    return _structured_content(response_data)


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


async def shopify_add_to_cart_tool(
    exec_context: ToolExecutionContext,
    business_url: str,
    line_items: list[dict[str, object]],
    cart_id: str | None = None,
    context: dict[str, object] | None = None,
) -> ToolResult:
    """Create or update a Shopify UCP cart."""
    app_config = _get_app_config(exec_context)
    normalized_items = _normalize_line_items(line_items)

    if cart_id:
        current = await _post_shopify_tool_call(
            app_config,
            business_url=business_url,
            tool_name="get_cart",
            arguments={"id": cart_id},
        )
        existing_cart = _cart_from_response(current)
        cart_payload = _cart_update_payload(existing_cart, normalized_items, context)
        response_data = await _post_shopify_tool_call(
            app_config,
            business_url=business_url,
            tool_name="update_cart",
            arguments={"id": cart_id, "cart": cart_payload},
        )
    else:
        cart_payload: dict[str, object] = {"line_items": normalized_items}
        if context is not None:
            cart_payload["context"] = context
        response_data = await _post_shopify_tool_call(
            app_config,
            business_url=business_url,
            tool_name="create_cart",
            arguments={"cart": cart_payload},
        )

    cart = _cart_from_response(response_data)
    return ToolResult(text=_cart_result_text(cart), data=response_data)


async def shopify_get_cart_tool(
    exec_context: ToolExecutionContext,
    business_url: str,
    cart_id: str,
) -> ToolResult:
    """Fetch a Shopify UCP cart."""
    app_config = _get_app_config(exec_context)
    response_data = await _post_shopify_tool_call(
        app_config,
        business_url=business_url,
        tool_name="get_cart",
        arguments={"id": cart_id},
    )
    cart = _cart_from_response(response_data)
    return ToolResult(text=_cart_result_text(cart), data=response_data)


async def shopify_transfer_checkout_to_human_tool(
    exec_context: ToolExecutionContext,
    business_url: str,
    cart_id: str,
) -> ToolResult:
    """Create a checkout session and return the human handoff URL."""
    app_config = _get_app_config(exec_context)
    response_data = await _post_shopify_tool_call(
        app_config,
        business_url=business_url,
        tool_name="create_checkout",
        arguments={"cart_id": cart_id},
        require_signed=True,
    )
    checkout = _checkout_from_response(response_data)
    continue_url = checkout.get("continue_url")
    checkout_id = checkout.get("id")
    status = checkout.get("status")
    if not isinstance(continue_url, str) or not continue_url:
        msg = "Shopify checkout response did not include a continue_url."
        raise ValueError(msg)

    details = [f"Checkout link: {continue_url}"]
    if isinstance(status, str):
        details.append(f"Status: {status}")
    if isinstance(checkout_id, str):
        details.append(f"Checkout ID: {checkout_id}")
    details.append("The buyer must complete payment on the merchant checkout page.")

    return ToolResult(text="\n".join(details), data=response_data)
