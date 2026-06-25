# Vendored UCP shopping schemas (2026-04-08)

These files are verbatim copies of the authoritative Universal Commerce Protocol shopping schemas,
vendored so the contract test (`tests/unit/tools/test_shopping_ucp_contract.py`) validates the
JSON-RPC request bodies our shopping tools emit against the spec itself rather than against
hand-authored types that could re-encode the same mistake.

| File                   | Source URL                                                       |
| ---------------------- | ---------------------------------------------------------------- |
| `mcp.openrpc.json`     | https://ucp.dev/2026-04-08/services/shopping/mcp.openrpc.json    |
| `checkout.json`        | https://ucp.dev/2026-04-08/schemas/shopping/checkout.json        |
| `cart.json`            | https://ucp.dev/2026-04-08/schemas/shopping/cart.json            |
| `types/line_item.json` | https://ucp.dev/2026-04-08/schemas/shopping/types/line_item.json |

The version (`2026-04-08`) matches `ucp_config.version` in `defaults.yaml` and the `schema` URLs
published in `build_ucp_profile`. To refresh, re-download from the URLs above into the matching
paths.

## How request shapes are derived

Each object schema annotates its properties with `ucp_request`, indicating whether a field is
`required`, `optional`, or `omit`ted for a given operation (`create`/`update`/`complete`). The
contract test derives a per-operation request schema from these annotations (dropping `omit` fields,
requiring `required` ones) and sets `additionalProperties: false`, so a request carrying a field the
spec does not define for that operation (e.g. a top-level `cart_id` on `create_checkout`) is
rejected.
