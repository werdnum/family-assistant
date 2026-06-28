# Vendored UCP shopping schemas (2026-04-08)

These files are verbatim copies of the authoritative Universal Commerce Protocol shopping schemas
(re-serialized with 2-space indentation), vendored so the contract test
(`tests/unit/tools/test_shopping_ucp_contract.py`) validates both the JSON-RPC request bodies our
shopping tools emit **and** the structuredContent shapes they parse against the spec itself, rather
than against hand-authored types that could re-encode the same mistake.

## What is vendored

The transitive `$ref` closure rooted at the shopping MCP OpenRPC document, the REST OpenAPI
document, and the cart/checkout schemas is vendored under this directory, mirroring the upstream
layout (top-level schemas at the root, shared types under `types/`). The roots are:

| File                | Source URL                                                     |
| ------------------- | -------------------------------------------------------------- |
| `mcp.openrpc.json`  | https://ucp.dev/2026-04-08/services/shopping/mcp.openrpc.json  |
| `rest.openapi.json` | https://ucp.dev/2026-04-08/services/shopping/rest.openapi.json |
| `cart.json`         | https://ucp.dev/2026-04-08/schemas/shopping/cart.json          |
| `checkout.json`     | https://ucp.dev/2026-04-08/schemas/shopping/checkout.json      |
| `ucp.json`          | https://ucp.dev/2026-04-08/schemas/ucp.json                    |

`rest.openapi.json` defines the REST transport's operations: each maps a UCP operation to an HTTP
verb + path (`create_cart` → `POST /carts`, `get_cart` → `GET /carts/{id}`, `update_cart` →
`PUT /carts/{id}`, `create_checkout` → `POST /checkout-sessions`), with the cart/checkout object as
the request body directly (not wrapped under a key) and the resource id in the path. Its request/
response bodies `$ref` the same `cart.json`/`checkout.json` schemas as the MCP transport, so the
contract test validates REST request bodies and paths against it the same way it does MCP
`tools/call` bodies.

Every other `*.json` here is reachable from those via `$ref` (e.g. `types/error_response.json`,
`types/totals.json`, `types/line_item.json`). Each carries its upstream `$id`; the contract test
builds a `referencing` registry from every document with an `$id` so `$ref`s resolve against the
vendored copies offline.

The version (`2026-04-08`) matches `ucp_config.version` in `defaults.yaml` and the `schema` URLs
published in `build_ucp_profile`. To refresh, re-download the closure from
`https://ucp.dev/2026-04-08/` into the matching paths.

## How request shapes are derived

Each object schema annotates its properties with `ucp_request`, indicating whether a field is
`required`, `optional`, or `omit`ted for a given operation (`create`/`update`/`complete`). The
contract test derives a per-operation request schema from these annotations (dropping `omit` fields,
requiring `required` ones) and sets `additionalProperties: false`, so a request carrying a field the
spec does not define for that operation (e.g. a top-level `cart_id` on `create_checkout`) is
rejected.

## How response shapes are checked

A cart/checkout method's MCP `result` is the cart/checkout object itself
(`cart_result = oneOf[cart, error_response]`), so its fields are top-level in `structuredContent` —
not nested under a `cart`/`checkout` key. The contract test validates the canned response fixtures
against `cart.json` / `checkout.json` (with the full ref closure resolved) and asserts the parser
rejects a legacy `{"cart": {...}}` wrapper, so the wrapper bug cannot return unnoticed.
