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
from urllib.parse import quote
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import SecretStr
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, SchemaRegistry

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


def _response_registry() -> SchemaRegistry:
    """Registry of every vendored JSON-Schema document (those carrying an ``$id``).

    Lets a UCP *response* be validated against the real ``cart``/``checkout``
    schemas with all their ``$ref``s resolved against the vendored closure — not
    a hand-authored stand-in that could re-encode the very wrapper mistake the
    validation is meant to catch. Each schema is keyed by its declared ``$id``.
    """
    registry: SchemaRegistry = Registry()
    for path in sorted(_SCHEMA_DIR.rglob("*.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        schema_id = contents.get("$id") if isinstance(contents, dict) else None
        if isinstance(schema_id, str):
            registry = registry.with_resource(
                schema_id,
                Resource.from_contents(contents, default_specification=DRAFT202012),
            )
    return registry


_RESPONSE_REGISTRY = _response_registry()


def _assert_response_conforms(
    schema_file: str, structured_content: dict[str, object]
) -> None:
    """Validate a UCP method's structuredContent against its vendored schema."""
    Draft202012Validator(
        _load_schema(schema_file), registry=_RESPONSE_REGISTRY
    ).validate(structured_content)


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


_REST_SPEC = _load_schema("rest.openapi.json")
_CART_SCHEMA_ID = "https://ucp.dev/2026-04-08/schemas/shopping/cart.json"
_CHECKOUT_SCHEMA_ID = "https://ucp.dev/2026-04-08/schemas/shopping/checkout.json"


def _rest_path_item(operation: str) -> tuple[str, str, dict[str, object]]:
    """Return ``(verb, path, path_item)`` for a REST operation in the spec."""
    paths = cast("dict[str, dict[str, object]]", _REST_SPEC["paths"])
    for path, item in paths.items():
        for method, op in item.items():
            if isinstance(op, dict) and op.get("operationId") == operation:
                return method.upper(), path, cast("dict[str, object]", item)
    msg = f"operation {operation} is not in the vendored REST OpenAPI document"
    raise AssertionError(msg)


def _rest_route(operation: str) -> tuple[str, str]:
    """The ``(verb, path template)`` the vendored REST OpenAPI defines for an op."""
    verb, path, _item = _rest_path_item(operation)
    return verb, path


def _rest_required_headers(operation: str) -> set[str]:
    """Header parameters the vendored REST OpenAPI marks required for an op.

    Path/query/cookie params are excluded; ``$ref``-ed parameter components are
    resolved so shared headers (``Request-Id``, ``Idempotency-Key``,
    ``UCP-Agent``) are picked up.
    """
    components = cast(
        "dict[str, dict[str, object]]",
        cast("dict[str, object]", _REST_SPEC["components"])["parameters"],
    )
    _verb, _path, item = _rest_path_item(operation)
    op = next(
        value
        for key, value in item.items()
        if isinstance(value, dict) and value.get("operationId") == operation
    )
    raw_params = cast("list[dict[str, object]]", item.get("parameters", [])) + cast(
        "list[dict[str, object]]", cast("dict[str, object]", op).get("parameters", [])
    )
    required: set[str] = set()
    for param in raw_params:
        ref = param.get("$ref")
        resolved = components[cast("str", ref).split("/")[-1]] if ref else param
        if resolved.get("in") == "header" and resolved.get("required"):
            required.add(cast("str", resolved["name"]))
    return required


def _rest_body_schema(
    operation: str, op: str, *, cart_checkout: bool = False
) -> dict[str, object]:
    """The request-body schema a REST operation must conform to.

    A REST cart/checkout request body is the object itself (not wrapped under a
    ``cart``/``checkout`` key the way the MCP ``tools/call`` arguments are), and
    the resource id travels in the path, never the body.
    """
    object_schema = {
        "create_cart": _SCHEMAS_BY_ID[_CART_SCHEMA_ID],
        "update_cart": _SCHEMAS_BY_ID[_CART_SCHEMA_ID],
        "create_checkout": _CART_CHECKOUT_SCHEMA
        if cart_checkout
        else _SCHEMAS_BY_ID[_CHECKOUT_SCHEMA_ID],
    }[operation]
    return _request_object_schema(object_schema, op, provided_params=frozenset({"id"}))


def _assert_rest_conforms(
    operation: str,
    captured: dict[str, object],
    *,
    expected_url: str,
    cart_checkout: bool = False,
) -> None:
    """Validate a captured REST request's verb, URL, and body against the spec."""
    verb, _path = _rest_route(operation)
    assert captured["method"] == verb
    assert captured["url"] == expected_url

    # Every spec-required REST header must be present (case-insensitive). This
    # binds the test to rest.openapi.json: Request-Id (all routes) and
    # Idempotency-Key (write routes), plus UCP-Agent.
    sent_headers = {
        name.lower(): value
        for name, value in cast("dict[str, str]", captured["headers"]).items()
    }
    for header in _rest_required_headers(operation):
        assert header.lower() in sent_headers, f"missing required REST header {header}"

    body = captured["body"]
    if operation in {"create_cart", "update_cart", "create_checkout"}:
        op = _METHOD_OP[operation]
        Draft202012Validator(
            _rest_body_schema(operation, op, cart_checkout=cart_checkout)
        ).validate(body)
    else:
        assert body is None


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
            signing_private_key=SecretStr(_private_key_pem()),
        ),
    )


def _context(app_config: AppConfig) -> ToolExecutionContext:
    return cast(
        "ToolExecutionContext",
        SimpleNamespace(processing_service=SimpleNamespace(app_config=app_config)),
    )


class _CapturingClient:
    posts: list[dict[str, object]] = []
    requests: list[dict[str, object]] = []
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

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None,
    ) -> httpx.Response:
        # REST transport calls go through client.request(verb, url, ...). Record
        # the verb, URL, headers, and decoded body so the contract test can
        # validate the request the REST path actually emits.
        self.requests.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": json.loads(content.decode("utf-8")) if content else None,
        })
        return self.post_responses.pop(0)


def _reset_client(
    monkeypatch: MonkeyPatch,
    *,
    post_responses: list[httpx.Response],
    profile_responses: list[httpx.Response] | None = None,
) -> None:
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _CapturingClient)
    _CapturingClient.posts = []
    _CapturingClient.requests = []
    _CapturingClient.post_responses = post_responses
    _CapturingClient.profile_responses = profile_responses or []


def _spec_totals() -> list[dict[str, object]]:
    # UCP totals MUST contain exactly one subtotal and one total entry.
    return [
        {"type": "subtotal", "display_text": "Subtotal", "amount": 3500},
        {"type": "total", "display_text": "Total", "amount": 3500},
    ]


def _spec_line_item() -> dict[str, object]:
    return {
        "id": "gid://shopify/LineItem/1",
        "item": {
            "id": "gid://shopify/ProductVariant/1",
            "title": "Socks",
            "price": 3500,
        },
        "quantity": 1,
        "totals": _spec_totals(),
    }


def _spec_cart(
    *, line_items: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "ucp": {"version": "2026-04-08"},
        "id": "gid://shopify/Cart/cart_abc123",
        "line_items": line_items if line_items is not None else [_spec_line_item()],
        "currency": "AUD",
        "totals": _spec_totals(),
        "continue_url": "https://shop.example.com/cart/c/cart_abc123",
    }


def _spec_checkout() -> dict[str, object]:
    return {
        "ucp": {"version": "2026-04-08", "payment_handlers": {}},
        "id": "gid://shopify/Checkout/checkout_abc123",
        "line_items": [_spec_line_item()],
        "currency": "AUD",
        "totals": _spec_totals(),
        "status": "requires_escalation",
        "links": [{"type": "terms_of_use", "url": "https://shop.example.com/terms"}],
        "continue_url": "https://shop.example.com/checkouts/c/checkout_abc123",
    }


def _cart_response(
    *, line_items: list[dict[str, object]] | None = None
) -> httpx.Response:
    # A UCP cart method returns the cart object itself as structuredContent
    # (cart_result = oneOf[cart, error_response]); it is NOT nested under a
    # "cart" key. Shaping the canned response like a real merchant's keeps the
    # parser exercised against the spec.
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": "rpc",
            "result": {"structuredContent": _spec_cart(line_items=line_items)},
        },
    )


def _checkout_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": "rpc",
            "result": {"structuredContent": _spec_checkout()},
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


def _mcp_cart_profile() -> httpx.Response:
    # A Shopify-style merchant advertising a same-origin MCP binding with the cart
    # capability, so the cart flow runs over the MCP JSON-RPC transport.
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
                "capabilities": {
                    "dev.ucp.shopping.cart": [{}],
                    "dev.ucp.shopping.checkout": [{}],
                },
            }
        },
    )


def _rest_profile(*, checkout_only: bool = False) -> httpx.Response:
    # THE ICONIC / Adore Beauty advertise a same-origin/same-site REST binding
    # instead of MCP. Adore is checkout-only (no cart capability).
    capabilities: dict[str, object] = {"dev.ucp.shopping.checkout": [{}]}
    if not checkout_only:
        capabilities["dev.ucp.shopping.cart"] = [{}]
    return httpx.Response(
        200,
        json={
            "ucp": {
                "services": {
                    "dev.ucp.shopping": [
                        {
                            "transport": "rest",
                            "endpoint": "https://shop.example.com/ucp/v1",
                        }
                    ]
                },
                "capabilities": capabilities,
            }
        },
    )


def _checkout_only_no_services_profile() -> httpx.Response:
    # A modern checkout-only merchant (UCP 2026-04-08) advertises only the
    # checkout capability and omits the `services` block entirely — no explicit
    # endpoint binding at all.
    return httpx.Response(
        200,
        json={
            "ucp": {
                "version": "2026-04-08",
                "capabilities": {"dev.ucp.shopping.checkout": [{}]},
            }
        },
    )


def _rest_cart_response(
    *, line_items: list[dict[str, object]] | None = None
) -> httpx.Response:
    # A REST cart response IS the cart object itself (cart_result =
    # oneOf[cart, error_response]) — no JSON-RPC envelope, no structuredContent.
    return httpx.Response(200, json=_spec_cart(line_items=line_items))


def _rest_checkout_response() -> httpx.Response:
    return httpx.Response(200, json=_spec_checkout())


async def test_create_cart_request_conforms_to_spec(monkeypatch: MonkeyPatch) -> None:
    _reset_client(
        monkeypatch,
        post_responses=[_cart_response()],
        profile_responses=[_mcp_cart_profile()],
    )

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
        profile_responses=[_mcp_cart_profile()],
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
        profile_responses=[_mcp_cart_profile()],
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


def test_cart_response_fixture_conforms_to_cart_schema() -> None:
    # The canned cart response is shaped like a real merchant's, so it must
    # validate against the vendored cart schema (with all $refs resolved).
    _assert_response_conforms("cart.json", _spec_cart())


def test_checkout_response_fixture_conforms_to_checkout_schema() -> None:
    _assert_response_conforms("checkout.json", _spec_checkout())


def test_cart_parser_reads_spec_shaped_response_and_rejects_legacy_wrapper() -> None:
    # The cart is the structuredContent itself (cart_result = oneOf[cart,
    # error_response]); a legacy ``{"cart": {...}}`` wrapper is not spec-valid
    # and the parser must not accept it. This couples the parser to the spec so
    # the wrapper bug cannot return unnoticed.
    spec_cart = _spec_cart()
    with pytest.raises(ValidationError):
        _assert_response_conforms("cart.json", {"cart": spec_cart})

    response: dict[str, object] = {
        "response": {"result": {"structuredContent": spec_cart}}
    }
    # SLF001: this test deliberately pins the module-private response parser to
    # the spec, so calling it directly is the point of the test.
    assert shopping._cart_from_response(response)["id"] == spec_cart["id"]

    wrapped: dict[str, object] = {
        "response": {"result": {"structuredContent": {"cart": spec_cart}}}
    }
    with pytest.raises(ValueError, match="did not include a cart"):
        # SLF001: same rationale — exercise the private parser directly.
        shopping._cart_from_response(wrapped)


async def test_rest_create_cart_request_conforms_to_spec(
    monkeypatch: MonkeyPatch,
) -> None:
    _reset_client(
        monkeypatch,
        post_responses=[_rest_cart_response()],
        profile_responses=[_rest_profile()],
    )

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 2}],
    )

    _assert_rest_conforms(
        "create_cart",
        _CapturingClient.requests[0],
        expected_url="https://shop.example.com/ucp/v1/carts",
    )


async def test_rest_update_cart_requests_conform_to_spec(
    monkeypatch: MonkeyPatch,
) -> None:
    existing = [{"quantity": 1, "item": {"id": "gid://shopify/ProductVariant/1"}}]
    cart_id = "gid://shopify/Cart/cart_abc123"
    _reset_client(
        monkeypatch,
        post_responses=[
            _rest_cart_response(line_items=existing),
            _rest_cart_response(),
        ],
        profile_responses=[_rest_profile()],
    )

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com",
        cart_id=cart_id,
        line_items=[{"variant_id": "gid://shopify/ProductVariant/2", "quantity": 1}],
    )

    quoted = quote(cart_id, safe="")
    # The opaque cart id is URL-encoded into the path so its slashes/colons
    # cannot escape the segment.
    assert "%2F" in quoted
    _assert_rest_conforms(
        "get_cart",
        _CapturingClient.requests[0],
        expected_url=f"https://shop.example.com/ucp/v1/carts/{quoted}",
    )
    _assert_rest_conforms(
        "update_cart",
        _CapturingClient.requests[1],
        expected_url=f"https://shop.example.com/ucp/v1/carts/{quoted}",
    )


async def test_rest_checkout_only_create_checkout_request_conforms_to_spec(
    monkeypatch: MonkeyPatch,
) -> None:
    _reset_client(
        monkeypatch,
        post_responses=[_rest_checkout_response()],
        profile_responses=[_rest_profile(checkout_only=True)],
    )

    await shopping.ucp_add_to_cart_tool(
        _context(_signed_config()),
        business_url="https://shop.example.com/products/serum",
        line_items=[{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 2}],
        context={"address_country": "AU"},
    )

    _assert_rest_conforms(
        "create_checkout",
        _CapturingClient.requests[0],
        expected_url="https://shop.example.com/ucp/v1/checkout-sessions",
    )


async def test_rest_checkout_only_no_services_resolves_signed_checkout(
    monkeypatch: MonkeyPatch,
) -> None:
    # A REST merchant advertising the checkout capability but no `services` block,
    # served under a /ucommerce subpath, must resolve end-to-end to a signed
    # create_checkout at the subpath-preserved URL — not fail into the Shopify
    # fallback. The root well-known redirects (same-origin) to the subpath one
    # that carries the profile, so the resolved endpoint is the /ucommerce parent.
    _reset_client(
        monkeypatch,
        post_responses=[_rest_checkout_response()],
        profile_responses=[
            httpx.Response(
                301,
                headers={
                    "Location": "https://shop.example.com/ucommerce/.well-known/ucp"
                },
            ),
            _checkout_only_no_services_profile(),
        ],
    )

    result = await shopping.ucp_add_to_cart_tool(
        _context(_signed_config()),
        business_url="https://shop.example.com/products/drill",
        line_items=[{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 2}],
    )

    create = _CapturingClient.requests[0]
    _assert_rest_conforms(
        "create_checkout",
        create,
        expected_url="https://shop.example.com/ucommerce/checkout-sessions",
    )
    # Checkout must be signed: the RFC 9421 Signature header rides on the request.
    assert _header(create, "Signature") is not None
    assert "Checkout link:" in result.get_text()


async def test_rest_cart_handoff_create_checkout_request_conforms_to_spec(
    monkeypatch: MonkeyPatch,
) -> None:
    existing = [{"quantity": 1, "item": {"id": "gid://shopify/ProductVariant/1"}}]
    cart_id = "gid://shopify/Cart/cart_abc123"
    _reset_client(
        monkeypatch,
        post_responses=[
            _rest_cart_response(line_items=existing),
            _rest_checkout_response(),
        ],
        profile_responses=[_rest_profile()],
    )

    await shopping.ucp_transfer_checkout_to_human_tool(
        _context(_signed_config()),
        business_url="https://shop.example.com",
        cart_id=cart_id,
    )

    quoted = quote(cart_id, safe="")
    _assert_rest_conforms(
        "get_cart",
        _CapturingClient.requests[0],
        expected_url=f"https://shop.example.com/ucp/v1/carts/{quoted}",
    )
    create = _CapturingClient.requests[1]
    # The cart handoff converts via cart_id inside the checkout object, valid
    # under the cart-capability "Checkout with Cart" extension.
    _assert_rest_conforms(
        "create_checkout",
        create,
        expected_url="https://shop.example.com/ucp/v1/checkout-sessions",
        cart_checkout=True,
    )
    assert cast("dict[str, object]", create["body"])["cart_id"] == cart_id


def _header(captured: dict[str, object], name: str) -> str | None:
    for key, value in cast("dict[str, str]", captured["headers"]).items():
        if key.lower() == name.lower():
            return value
    return None


async def test_rest_write_request_sends_uuid_request_and_idempotency_headers(
    monkeypatch: MonkeyPatch,
) -> None:
    # The REST OpenAPI requires Request-Id (all routes) and Idempotency-Key
    # (write routes) as UUIDs. The unsigned create_cart path must emit both as
    # valid UUIDs; a spec-enforcing merchant rejects the call otherwise.
    _reset_client(
        monkeypatch,
        post_responses=[_rest_cart_response()],
        profile_responses=[_rest_profile()],
    )

    await shopping.ucp_add_to_cart_tool(
        _context(AppConfig(server_url="https://assistant.example")),
        business_url="https://shop.example.com/products/sweater",
        line_items=[{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 2}],
    )

    write = _CapturingClient.requests[0]
    request_id = _header(write, "Request-Id")
    idempotency_key = _header(write, "Idempotency-Key")
    assert request_id is not None
    assert idempotency_key is not None
    # Parsing as a UUID asserts the spec's `format: uuid` is honoured.
    assert str(UUID(request_id)) == request_id
    assert str(UUID(idempotency_key)) == idempotency_key
