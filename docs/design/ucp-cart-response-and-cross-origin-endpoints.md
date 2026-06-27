# UCP cart-response parsing & cross-host endpoint trust

## Context

Live testing of the UCP shopping tools against Australian retailers surfaced that the tools could
not complete a cart against any real merchant, even ones we nominally support. Investigation against
the vendored UCP spec found three issues, fixed here. A separate gap (no REST transport) is recorded
as follow-up.

## 1. Cart response parsing (P0 bug)

`_cart_from_response` read the cart from `structuredContent["cart"]`, but a UCP cart method's MCP
result **is** the cart object itself: the OpenRPC `create_cart`/`get_cart`/`update_cart` result is
`cart_result = oneOf[cart, error_response]`, so the cart's `id`/`line_items` are top-level in
`structuredContent`, not nested under a `cart` key. The wrapper lookup returned `None` and raised
"UCP response did not include a cart." on every spec-compliant response. The fix reads the cart
directly from `structuredContent` (checkout parsing was already correct, which is why only carts
broke).

### Why tests didn't catch it

The contract test validated only the JSON-RPC **request** bodies our tools emit. Response parsing
was exercised against hand-authored fixtures that nested the cart under `cart` — the same wrong
assumption as the parser — so they agreed and passed. This is the "fixtures re-encode the mistake"
trap `tests/data/ucp/2026-04-08/SOURCE.md` warns about, applied to requests only.

The fix vendors the full transitive `$ref` closure of the cart/checkout schemas (57 documents) and
validates response fixtures against `cart.json`/`checkout.json` with all refs resolved, plus a test
asserting the parser rejects a legacy `{"cart": {...}}` wrapper. Response shapes are now spec-bound.

## 2. Cross-host endpoint trust (same-site + platform allowlist)

Previously the tools accepted only a **same-origin** advertised MCP endpoint, else fell back to the
Shopify `/api/ucp/mcp` convention on the storefront origin. That broke two real shapes:

- **Same-site, different subdomain** — e.g. THE ICONIC advertises `eve.theiconic.com.au` for the
  `www.theiconic.com.au` storefront. Still the merchant's own site.
- **Cross-site to a platform backend** — a Shopify store on a custom domain advertises its
  `*.myshopify.com` shop host, which is cross-site to the storefront.

`MerchantUCPProfile.usable_mcp_endpoint(trusted_suffixes=...)` now accepts an advertised endpoint
when it is same-origin, **same-site** (same registrable domain via the public suffix list, so
multi-label suffixes like `com.au` are handled), or hosted on a configured **trusted platform
suffix** (`ucp_config.trusted_endpoint_suffixes`, default `["myshopify.com"]`). The browser
shoppable-hint probe uses the same predicate so the hint matches what the tools will actually do.

### Security rationale

The profile is untrusted merchant-controlled metadata, so the signed POST must not be redirected to
an arbitrary host (SSRF). This design keeps that bound:

- **Same-site** only extends trust to the merchant's own registrable domain — the site the user
  chose to shop at. No new authority beyond what same-origin already implied for that merchant. The
  registrable domain is resolved against a **vendored current Public Suffix List**
  (`src/family_assistant/data/public_suffix_list.dat`, including the PRIVATE section) rather than
  `publicsuffix2`'s bundled 2019 snapshot: a stale list collapses co-tenants on multi-tenant
  suffixes added since 2019 (`vercel.app`, `pages.dev`, `fly.dev`) to the platform domain, which
  would let a profile on `foo.vercel.app` pass off `bar.vercel.app` as same-site. Hosts with no
  public-suffix match (IP literals, internal names) and malformed-port URLs never resolve to a
  registrable domain and so never match. Refresh the vendored list periodically from
  <https://publicsuffix.org/list/public_suffix_list.dat>.
- **Trusted suffixes** are an explicit, operator-controlled allowlist of public commerce-platform
  backends. `*.myshopify.com` resolves to Shopify's public edge and cannot be pointed at our
  internal network, so it neutralizes the SSRF-to-internal-host vector. The residual risk is a
  "confused deputy" POST (a non-sensitive `create_cart` body, signature bound per RFC 9421 to that
  method/body/endpoint) to the wrong shop — low blast radius. Matching is whole-label anchored
  (`status-anxiety-2.myshopify.com` matches; `notmyshopify.com` / `myshopify.com.evil.com` do not)
  and HTTPS-only.

### Out of scope

DNS rebinding of an attacker's own public domain to a private IP is not newly introduced here (the
prior same-origin path posted to the attacker-influenced storefront origin too) and is not addressed
in this change. A private-IP/internal-host guard on the shopping POST path remains possible future
hardening.

## Follow-up: REST transport

THE ICONIC and Adore Beauty advertise `transport: "rest"`, not `mcp`. The client only speaks the MCP
JSON-RPC transport, so those merchants still fall back to a non-existent `/api/ucp/mcp` path and
fail. Adding a REST-transport client (the `dev.ucp.shopping` `rest` binding /
`rest.openapi.json`) is the unlock for REST-only merchants and is tracked separately.
