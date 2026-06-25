"""Contract tests: the JSON-RPC bodies our shopping tools emit must conform to
the vendored UCP shopping schemas.

Rather than re-encoding the request shapes as hand-written types (which could
repeat the very mistake they are meant to catch), this validates the bodies our
tools actually send against the authoritative schemas vendored under
``tests/data/ucp/2026-04-08`` (see that directory's ``SOURCE.md``). The
per-operation request schema is derived from each property's ``ucp_request``
annotation (drop ``omit`` fields, require ``required`` ones) with
``additionalProperties: false``, so a body carrying a field the spec does not
define for that operation — e.g. a top-level ``cart_id`` on ``create_checkout``
— is rejected.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from family_assistant.config_models import AppConfig, UCPConfig
from family_assistant.tools import shopping

if TYPE_CHECKING:
    from pytest import MonkeyPatch

    from family_assistant.tools.types import ToolExecutionContext


_SCHEMA_DIR = Path(__file__).parents[2] / "data" / "ucp" / "2026-04-08"


def _load_schema(*parts: str) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads(_SCHEMA_DIR.joinpath(*parts).read_text(encoding="utf-8")),
    )


_OPENRPC = _load_schema("mcp.openrpc.json")
_SCHEMAS_BY_ID: dict[str, dict[str, object]] = {
    cast("str", schema["$id"]): schema
    for schema in (
        _load_schema("checkout.json"),
        _load_schema("cart.json"),
        _load_schema("types", "line_item.json"),
    )
}
# The cart capability extends create_checkout with cart_id for cart-to-checkout
# conversion ("Checkout with Cart"); a cart handoff is validated against it.
_CART_CHECKOUT_SCHEMA = cast(
    "dict[str, dict[str, object]]", _load_schema("cart.json")["$defs"]
)["checkout"]
# Which lifecycle operation each method's object param represents.
_METHOD_OP = {
    "create_checkout": "create",
    "create_cart": "create",
    "update_cart": "update",
    "get_cart": "create",
}


def _request_mode(annotation: object, op: str) -> str | None:
    """Resolve a property's ``ucp_request`` annotation for ``op``.

    Returns ``None`` when the property carries no annotation, leaving the caller
    to fall back to the schema's base ``required`` set.
    """
    if isinstance(annotation, str):
        return annotation
    if isinstance(annotation, dict):
        return cast("str | None", annotation.get(op, "omit"))
    return None


def _value_schema(prop: dict[str, object], op: str) -> dict[str, object]:
    ref = prop.get("$ref")
    if isinstance(ref, str):
        target = _SCHEMAS_BY_ID.get(ref)
        if target is not None:
            return _request_object_schema(target, op)
        # A referenced type we did not vendor (e.g. item.json): accept any value
        # so the test validates the fields we construct, not ones we pass through.
        return {}
    if prop.get("type") == "array":
        items = prop.get("items")
        item_schema = _value_schema(items, op) if isinstance(items, dict) else {}
        return {"type": "array", "items": item_schema}
    return {key: prop[key] for key in ("type", "enum") if key in prop}


def _request_object_schema(
    schema: dict[str, object],
    op: str,
    *,
    provided_params: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Derive the request-time JSON Schema for a UCP object schema.

    ``allOf`` members (used by the cart capability's "Checkout with Cart"
    extension, which composes the base checkout with a ``cart_id`` field) are
    resolved and merged, so a composed schema contributes every branch's
    request fields.
    """
    members: list[dict[str, object]] = []
    for sub in cast("list[dict[str, object]]", schema.get("allOf", [])):
        ref = sub.get("$ref")
        members.append(
            _SCHEMAS_BY_ID[ref]
            if isinstance(ref, str) and ref in _SCHEMAS_BY_ID
            else sub
        )
    members.append(schema)
    request_props: dict[str, object] = {}
    required: list[str] = []
    for member in members:
        properties = cast("dict[str, dict[str, object]]", member.get("properties", {}))
        base_required = set(cast("list[str]", member.get("required", [])))
        for name, prop in properties.items():
            mode = _request_mode(prop.get("ucp_request"), op)
            if mode is None:
                mode = "required" if name in base_required else "optional"
            if mode == "omit":
                continue
            request_props[name] = _value_schema(prop, op)
            # A field the MCP method also supplies as its own top-level param
            # (the cart id on update_cart) need not be repeated inside the object.
            if (
                mode == "required"
                and name not in provided_params
                and name not in required
            ):
                required.append(name)
    return {
        "type": "object",
        "properties": request_props,
        "required": required,
        "additionalProperties": False,
    }


def _method(name: str) -> dict[str, object]:
    for method in cast("list[dict[str, object]]", _OPENRPC["methods"]):
        if method.get("name") == name:
            return method
    msg = f"method {name} is not in the vendored OpenRPC document"
    raise AssertionError(msg)


def _arguments_schema(
    method_name: str, *, cart_checkout: bool = False
) -> dict[str, object]:
    """Build the JSON Schema for a method's ``tools/call`` arguments object.

    ``cart_checkout`` selects the cart-capability "Checkout with Cart" extension
    (which permits ``cart_id``) for the ``checkout`` param, used by the cart
    handoff; the checkout-only path validates against the base checkout schema.
    """
    method = _method(method_name)
    op = _METHOD_OP[method_name]
    params = cast("list[dict[str, object]]", method["params"])
    param_names = frozenset(cast("str", param["name"]) for param in params)
    props: dict[str, object] = {}
    required: list[str] = []
    for param in params:
        name = cast("str", param["name"])
        schema = cast("dict[str, object]", param.get("schema", {}))
        ref = schema.get("$ref")
        if name == "meta":
            props[name] = {"type": "object"}
        elif isinstance(ref, str) and ref in _SCHEMAS_BY_ID:
            object_schema = (
                _CART_CHECKOUT_SCHEMA
                if cart_checkout and name == "checkout"
                else _SCHEMAS_BY_ID[ref]
            )
            props[name] = _request_object_schema(
                object_schema, op, provided_params=param_names
            )
        elif schema.get("type") == "string":
            props[name] = {"type": "string"}
        else:
            props[name] = {}
        if param.get("required"):
            required.append(name)
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _assert_conforms(
    method_name: str, body: dict[str, object], *, cart_checkout: bool = False
) -> None:
    params = cast("dict[str, object]", body["params"])
    assert params["name"] == method_name
    arguments = cast("dict[str, object]", params["arguments"])
    Draft202012Validator(
        _arguments_schema(method_name, cart_checkout=cart_checkout)
    ).validate(arguments)


def _private_key_pem() -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _signed_config() -> AppConfig:
    return AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )


def _context(app_config: AppConfig) -> ToolExecutionContext:
    return cast(
        "ToolExecutionContext",
        SimpleNamespace(processing_service=SimpleNamespace(app_config=app_config)),
    )


class _CapturingClient:
    posts: list[dict[str, object]] = []
    post_responses: list[httpx.Response] = []
    profile_responses: list[httpx.Response] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _CapturingClient:
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
        self.posts.append(json.loads((content or b"{}").decode("utf-8")))
        return self.post_responses.pop(0)


def _reset_client(
    monkeypatch: MonkeyPatch,
    *,
    post_responses: list[httpx.Response],
    profile_responses: list[httpx.Response] | None = None,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _CapturingClient)
    _CapturingClient.posts = []
    _CapturingClient.post_responses = post_responses
    _CapturingClient.profile_responses = profile_responses or []


def _cart_response(
    *, line_items: list[dict[str, object]] | None = None
) -> httpx.Response:
    cart: dict[str, object] = {
        "id": "gid://shopify/Cart/cart_abc123",
        "line_items": line_items if line_items is not None else [],
        "continue_url": "https://shop.example.com/cart/c/cart_abc123",
    }
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": "rpc",
            "result": {"structuredContent": {"cart": cart}},
        },
    )


def _checkout_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": "rpc",
            "result": {
                "structuredContent": {
                    "id": "gid://shopify/Checkout/checkout_abc123",
                    "status": "requires_escalation",
                    "continue_url": "https://shop.example.com/checkouts/c/checkout_abc123",
                }
            },
        },
    )


def _checkout_only_profile() -> httpx.Response:
    return httpx.Response(
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
                },
                "capabilities": {"dev.ucp.shopping.checkout": [{}]},
            }
        },
    )


async def test_create_cart_request_conforms_to_spec(monkeypatch: MonkeyPatch) -> None:
    _reset_client(monkeypatch, post_responses=[_cart_response()])

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 2}],
    )

    _assert_conforms("create_cart", _CapturingClient.posts[0])


async def test_update_cart_requests_conform_to_spec(monkeypatch: MonkeyPatch) -> None:
    existing = [{"quantity": 1, "item": {"id": "gid://shopify/ProductVariant/1"}}]
    _reset_client(
        monkeypatch,
        post_responses=[_cart_response(line_items=existing), _cart_response()],
    )

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com",
        cart_id="gid://shopify/Cart/cart_abc123",
        line_items=[{"variant_id": "gid://shopify/ProductVariant/2", "quantity": 1}],
    )

    _assert_conforms("get_cart", _CapturingClient.posts[0])
    _assert_conforms("update_cart", _CapturingClient.posts[1])


async def test_checkout_only_create_checkout_request_conforms_to_spec(
    monkeypatch: MonkeyPatch,
) -> None:
    _reset_client(
        monkeypatch,
        post_responses=[_checkout_response()],
        profile_responses=[_checkout_only_profile()],
    )

    await shopping.ucp_add_to_cart_tool(
        _context(_signed_config()),
        business_url="https://shop.example.com/products/serum",
        line_items=[{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 2}],
        context={"address_country": "AU"},
    )

    _assert_conforms("create_checkout", _CapturingClient.posts[0])


async def test_cart_handoff_create_checkout_request_conforms_to_spec(
    monkeypatch: MonkeyPatch,
) -> None:
    existing = [{"quantity": 1, "item": {"id": "gid://shopify/ProductVariant/1"}}]
    _reset_client(
        monkeypatch,
        post_responses=[_cart_response(line_items=existing), _checkout_response()],
    )

    await shopping.ucp_transfer_checkout_to_human_tool(
        _context(_signed_config()),
        business_url="https://shop.example.com",
        cart_id="gid://shopify/Cart/cart_abc123",
    )

    _assert_conforms("get_cart", _CapturingClient.posts[0])
    create = _CapturingClient.posts[1]
    # The cart handoff converts via cart_id, valid under the cart extension.
    _assert_conforms("create_checkout", create, cart_checkout=True)
    checkout = cast(
        "dict[str, object]",
        cast("dict[str, object]", create["params"])["arguments"],
    )["checkout"]
    assert cast("dict[str, object]", checkout)["cart_id"] == (
        "gid://shopify/Cart/cart_abc123"
    )


def test_contract_rejects_the_shapes_these_fixes_corrected() -> None:
    # Proof the contract catches the two real bugs: a top-level cart_id (the
    # pre-existing handoff) and line items not nested under `checkout` (the
    # first checkout-only attempt). Both must fail validation.
    validator = Draft202012Validator(_arguments_schema("create_checkout"))
    line_items = [{"quantity": 1, "item": {"id": "gid://shopify/ProductVariant/1"}}]

    with pytest.raises(ValidationError):
        validator.validate({"meta": {}, "cart_id": "gid://shopify/Cart/cart_abc123"})
    with pytest.raises(ValidationError):
        validator.validate({"meta": {}, "line_items": line_items})

    # The corrected base shape validates.
    validator.validate({"meta": {}, "checkout": {"line_items": line_items}})

    # cart_id is only valid inside checkout under the cart-capability extension:
    # rejected against the base schema, accepted against the extended one.
    with pytest.raises(ValidationError):
        validator.validate({
            "meta": {},
            "checkout": {"cart_id": "c1", "line_items": line_items},
        })
    cart_validator = Draft202012Validator(
        _arguments_schema("create_checkout", cart_checkout=True)
    )
    cart_validator.validate({
        "meta": {},
        "checkout": {"cart_id": "c1", "line_items": line_items},
    })
