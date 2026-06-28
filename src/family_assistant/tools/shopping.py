"""Shopping tools backed by UCP merchant shopping endpoints.

These tools work with any merchant that advertises a Universal Commerce Protocol
(UCP) profile at ``/.well-known/ucp``. The merchant's shopping endpoint and its
transport (MCP JSON-RPC or REST) are discovered from that profile; when a
merchant does not advertise a usable binding, the tools fall back to the Shopify
MCP convention (``/api/ucp/mcp``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import uuid4

import httpx

from family_assistant.services.ucp import (
    MCP_TRANSPORT,
    REST_TRANSPORT,
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
    """The endpoint to use for a merchant, its transport, and cart/checkout shape.

    ``transport`` is ``MCP_TRANSPORT`` or ``REST_TRANSPORT`` and selects whether
    operations are driven over the MCP JSON-RPC ``tools/call`` body or the REST
    verbs/paths from ``rest.openapi.json``.

    ``checkout_only`` is True when the merchant advertises the checkout
    capability but not the cart capability, meaning its endpoint has no cart
    methods and a cart must be skipped in favour of a direct checkout.
    """

    endpoint: str
    transport: str
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
    business_url: str,
    *,
    client: httpx.AsyncClient,
    trusted_suffixes: tuple[str, ...] = (),
) -> _ResolvedMerchant:
    """Resolve the merchant's shopping endpoint and transport for ``business_url``.

    Discovers the endpoints from the merchant's ``/.well-known/ucp`` profile and
    falls back to the Shopify MCP convention (``/api/ucp/mcp``) when the merchant
    advertises no usable shopping binding. The returned ``transport`` (MCP or
    REST) selects how the endpoint is driven. The ``checkout_only`` flag reflects
    the discovered capabilities so callers can bypass the cart for checkout-only
    merchants — but only when the profile's own endpoint is used; on fallback the
    flag is cleared, since the capability described a binding that was not
    adopted.

    A binding is usable when it is same-origin, same-site (a sibling subdomain of
    ``business_url``'s registrable domain), or hosted on a configured trusted
    platform suffix (``trusted_suffixes``, e.g. ``myshopify.com`` for Shopify
    storefronts on custom domains). Every advertised binding — MCP or REST — is
    considered, so a usable one is still selected when an unusable binding is
    listed ahead of it. Anything else is ignored: the profile is untrusted
    merchant-controlled metadata, so an arbitrary cross-host endpoint could
    otherwise redirect the signed request at an unrelated/internal host — an SSRF
    vector. Discovery is also given a short timeout so a store that does not
    publish a profile (and may tarpit the well-known path) falls back promptly
    instead of stalling the request.
    """
    origin = merchant_origin(business_url)
    if origin is None:
        msg = "business_url must be an https merchant origin."
        raise ValueError(msg)
    profile = await discover_merchant_ucp_profile(
        business_url, client=client, timeout=UCP_DISCOVERY_TIMEOUT_SECONDS
    )
    if profile is not None:
        usable = profile.usable_shopping_endpoint(trusted_suffixes=trusted_suffixes)
        if usable is not None:
            return _ResolvedMerchant(
                endpoint=usable.endpoint,
                transport=usable.transport,
                checkout_only=_is_checkout_only(profile),
            )
        if profile.mcp_endpoints or profile.rest_endpoints:
            logger.warning(
                "Ignoring %d untrusted cross-host UCP endpoint(s) advertised by %s",
                len(profile.mcp_endpoints) + len(profile.rest_endpoints),
                origin,
            )
    # No profile, or only unusable (untrusted cross-host) bindings: fall back to
    # the Shopify MCP convention. That endpoint supports carts, so the profile's
    # checkout-only capability — which described the binding we just refused —
    # must not carry over and make the caller skip the cart here.
    return _ResolvedMerchant(
        endpoint=f"{origin}{SHOPIFY_FALLBACK_MCP_PATH}",
        transport=MCP_TRANSPORT,
        checkout_only=False,
    )


async def _resolve_endpoint_for(
    business_url: str, *, trusted_suffixes: tuple[str, ...] = ()
) -> _ResolvedMerchant:
    """Resolve the merchant MCP endpoint once, with a dedicated short client.

    Resolving up front (rather than inside each POST) means a multi-step
    operation — such as a cart update that reads then writes — uses one
    consistent endpoint instead of rediscovering per call, where a flaky second
    discovery could split reads and writes across different endpoints.
    """
    async with httpx.AsyncClient(timeout=UCP_DISCOVERY_TIMEOUT_SECONDS) as client:
        return await _resolve_ucp_endpoint(
            business_url, client=client, trusted_suffixes=trusted_suffixes
        )


def _trusted_endpoint_suffixes(app_config: AppConfig) -> tuple[str, ...]:
    return tuple(app_config.ucp_config.trusted_endpoint_suffixes)


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


# REST transport: each UCP operation maps to a verb + path template from the
# shopping ``rest.openapi.json``. The object body (cart/checkout) is sent
# directly — REST does not wrap it under a key the way the MCP tools/call
# arguments do — and the resource id travels in the path, not the body.
_REST_ROUTES: dict[str, tuple[str, str]] = {
    "create_cart": ("POST", "/carts"),
    "get_cart": ("GET", "/carts/{id}"),
    "update_cart": ("PUT", "/carts/{id}"),
    "create_checkout": ("POST", "/checkout-sessions"),
}

# Which tools/call argument key wraps the object payload for each MCP method; the
# resource id (when present) is a sibling top-level argument, not nested.
_MCP_OBJECT_KEY: dict[str, str | None] = {
    "create_cart": "cart",
    "get_cart": None,
    "update_cart": "cart",
    "create_checkout": "checkout",
}


def _mcp_arguments(
    operation: str, resource_id: str | None, payload: dict[str, object] | None
) -> dict[str, object]:
    """Build the MCP ``tools/call`` arguments for a UCP operation.

    Re-creates the transport-specific shape the merchant's MCP endpoint expects:
    the id as a top-level ``id`` argument and the object payload wrapped under
    its ``cart``/``checkout`` key.
    """
    arguments: dict[str, object] = {}
    if resource_id is not None:
        arguments["id"] = resource_id
    object_key = _MCP_OBJECT_KEY[operation]
    if object_key is not None and payload is not None:
        arguments[object_key] = payload
    return arguments


def _rest_route(
    base_endpoint: str, operation: str, resource_id: str | None
) -> tuple[str, str]:
    """Return the ``(verb, url)`` for a UCP operation on a REST endpoint.

    The resource id is URL-encoded into the path (cart/checkout ids are opaque
    and may contain ``/`` and ``:``) so it cannot escape its path segment.
    """
    verb, path_template = _REST_ROUTES[operation]
    if "{id}" in path_template:
        if not resource_id:
            msg = f"UCP REST {operation} requires a resource id."
            raise ValueError(msg)
        path = path_template.replace("{id}", quote(resource_id, safe=""))
    else:
        path = path_template
    return verb, f"{base_endpoint.rstrip('/')}{path}"


def _rest_error_text(body: dict[str, object]) -> str | None:
    """First message text from a UCP REST ``error_response`` body, if any."""
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                return _message_text(message)
    return None


def _raise_for_rest_failure(
    response: httpx.Response, response_data: dict[str, object]
) -> None:
    """Raise on a hard REST failure (HTTP error status).

    A non-2xx response carries an ``error_response`` body (``cart_result =
    oneOf[cart, error_response]``), so its first message gives a clearer reason
    than the bare status. Recoverable, in-resource failures (a 2xx cart/checkout
    carrying error messages) are left for the shared cart/checkout parsers.
    """
    if response.is_error:
        msg = (
            _rest_error_text(response_data) or f"UCP HTTP error {response.status_code}."
        )
        raise ValueError(msg)


async def _post_ucp_rest_call(
    app_config: AppConfig,
    *,
    base_endpoint: str,
    operation: str,
    resource_id: str | None = None,
    payload: dict[str, object] | None = None,
    require_signed: bool = False,
) -> dict[str, object]:
    """Drive a UCP operation over the REST transport and normalize the response.

    Signs the request with the same RFC 9421 helper as the MCP path (when a
    signing key is configured), then re-nests the REST resource under the MCP
    ``result.structuredContent`` shape so the shared cart/checkout parsers work
    unchanged — a REST cart/checkout response IS the resource object.
    """
    verb, url = _rest_route(base_endpoint, operation, resource_id)
    body = payload if verb in {"POST", "PUT"} else None
    # The REST OpenAPI marks Request-Id required on every route and
    # Idempotency-Key required on the write routes (POST/PUT), so a
    # spec-enforcing merchant would reject a call missing them. Generate both:
    # the signing helper folds the idempotency key into the signed headers (and
    # already supplies one for state-changing methods), while Request-Id rides
    # as an unsigned tracing header on both the signed and unsigned paths.
    request_id = str(uuid4())
    idempotency_key = str(uuid4()) if verb in {"POST", "PUT"} else None

    if require_signed:
        _require_signing_config(app_config)

    if has_ucp_signing_key(app_config):
        try:
            signed_request = sign_ucp_request(
                app_config,
                method=verb,
                url=url,
                body=body,
                idempotency_key=idempotency_key,
                additional_headers={"Request-Id": request_id},
            )
        except UCPConfigurationError as exc:
            raise ValueError(str(exc)) from exc
        request_headers = signed_request.headers
        request_body = signed_request.body
    else:
        request_headers = {
            "UCP-Agent": ucp_agent_header(app_config),
            "Request-Id": request_id,
        }
        if idempotency_key is not None:
            request_headers["Idempotency-Key"] = idempotency_key
        request_body = None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            request_body = _json_bytes(body)

    logger.info("Calling UCP REST %s %s", verb, url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            verb, url, headers=request_headers, content=request_body
        )

    try:
        response_data = response.json()
    except json.JSONDecodeError as exc:
        msg = f"UCP response was not JSON: HTTP {response.status_code}"
        raise ValueError(msg) from exc

    if not isinstance(response_data, dict):
        msg = "UCP response JSON must be an object."
        raise ValueError(msg)
    _raise_for_rest_failure(response, response_data)

    return {
        "status_code": response.status_code,
        "response": {"result": {"structuredContent": response_data}},
        "request": {
            "url": url,
            "tool_name": operation,
            "signed": has_ucp_signing_key(app_config),
        },
    }


async def _ucp_call(
    app_config: AppConfig,
    *,
    resolved: _ResolvedMerchant,
    operation: str,
    resource_id: str | None = None,
    payload: dict[str, object] | None = None,
    require_signed: bool = False,
) -> dict[str, object]:
    """Dispatch a UCP operation over the merchant's resolved transport.

    The same semantic operation (``create_cart``/``get_cart``/``update_cart``/
    ``create_checkout``) is mapped to a JSON-RPC ``tools/call`` body for an MCP
    endpoint or to a REST verb/path for a REST endpoint; both return the shared
    normalized response shape the cart/checkout parsers consume.
    """
    if resolved.transport == REST_TRANSPORT:
        return await _post_ucp_rest_call(
            app_config,
            base_endpoint=resolved.endpoint,
            operation=operation,
            resource_id=resource_id,
            payload=payload,
            require_signed=require_signed,
        )
    return await _post_ucp_tool_call(
        app_config,
        endpoint=resolved.endpoint,
        tool_name=operation,
        arguments=_mcp_arguments(operation, resource_id, payload),
        require_signed=require_signed,
    )


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
    # A UCP cart method returns the cart object itself as the MCP
    # structuredContent (the OpenRPC result is cart_result = oneOf[cart,
    # error_response]); the cart's id/line_items are top-level, not nested under
    # a "cart" key. Reading a non-existent wrapper made every spec-compliant
    # response report "did not include a cart".
    cart = _structured_content(response_data)
    if not cart:
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
    resolved: _ResolvedMerchant,
    checkout_payload: dict[str, object],
) -> ToolResult:
    """Make a signed ``create_checkout`` call and render the handoff result.

    Shared by the cart-based checkout handoff and the checkout-only add-to-cart
    path; ``checkout_payload`` is the ``checkout`` object (holding line items and
    any supported checkout fields) the UCP create_checkout binding expects.
    """
    response_data = await _ucp_call(
        app_config,
        resolved=resolved,
        operation="create_checkout",
        payload=checkout_payload,
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
    resolved: _ResolvedMerchant,
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
    # The UCP create_checkout binding takes a `checkout` object with line items
    # (and any supported checkout fields) inside it, mirroring how create_cart
    # carries its payload as the cart object.
    checkout_payload: dict[str, object] = {"line_items": normalized_items}
    if context is not None:
        checkout_payload["context"] = context
    return await _create_checkout_session(
        app_config, resolved=resolved, checkout_payload=checkout_payload
    )


def _checkout_input_from_cart(
    cart: dict[str, object], cart_id: str
) -> dict[str, object]:
    """Build a create_checkout ``checkout`` object that converts an existing cart.

    The cart capability extends create_checkout with ``cart_id`` (see the
    "Checkout with Cart" schema, ``cart.json#/$defs/checkout``): the business
    uses the cart's stored contents for conversion and ignores overlapping
    payload fields, preserving server-side cart state (discounts, holds). The
    cart's line items are still sent because the base checkout schema requires
    them; buyer/context/signals are carried over for merchants that read them.
    """
    line_items = _merge_cart_line_items(cart, [])
    if not line_items:
        msg = "Cart has no line items to check out."
        raise ValueError(msg)
    checkout_input: dict[str, object] = {"cart_id": cart_id, "line_items": line_items}
    for field in ("buyer", "context", "signals"):
        value = cart.get(field)
        if isinstance(value, dict):
            checkout_input[field] = value
    return checkout_input


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
    resolved = await _resolve_endpoint_for(
        business_url, trusted_suffixes=_trusted_endpoint_suffixes(app_config)
    )

    if resolved.checkout_only:
        return await _add_to_cart_checkout_only(
            app_config,
            resolved=resolved,
            normalized_items=normalized_items,
            cart_id=cart_id,
            context=context,
        )

    if cart_id:
        current = await _ucp_call(
            app_config,
            resolved=resolved,
            operation="get_cart",
            resource_id=cart_id,
        )
        existing_cart = _cart_from_response(current)
        cart_payload = _cart_update_payload(existing_cart, normalized_items, context)
        response_data = await _ucp_call(
            app_config,
            resolved=resolved,
            operation="update_cart",
            resource_id=cart_id,
            payload=cart_payload,
        )
    else:
        cart_payload: dict[str, object] = {"line_items": normalized_items}
        if context is not None:
            cart_payload["context"] = context
        response_data = await _ucp_call(
            app_config,
            resolved=resolved,
            operation="create_cart",
            payload=cart_payload,
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
    resolved = await _resolve_endpoint_for(
        business_url, trusted_suffixes=_trusted_endpoint_suffixes(app_config)
    )
    response_data = await _ucp_call(
        app_config,
        resolved=resolved,
        operation="get_cart",
        resource_id=cart_id,
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
    resolved = await _resolve_endpoint_for(
        business_url, trusted_suffixes=_trusted_endpoint_suffixes(app_config)
    )
    # The cart capability extends create_checkout with cart_id for
    # cart-to-checkout conversion, so read the cart and hand off its cart_id
    # (plus its contents) inside the checkout object.
    cart_response = await _ucp_call(
        app_config,
        resolved=resolved,
        operation="get_cart",
        resource_id=cart_id,
    )
    cart = _cart_from_response(cart_response)
    return await _create_checkout_session(
        app_config,
        resolved=resolved,
        checkout_payload=_checkout_input_from_cart(cart, cart_id),
    )
