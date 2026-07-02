# UCP cart-response parsing & cross-host endpoint trust

## Context

Live testing of the UCP shopping tools against Australian retailers surfaced that the tools could
not complete a cart against any real merchant, even ones we nominally support. Investigation against
the vendored UCP spec found three issues, fixed here. Section 3 was added as a follow-up: it closed
a gap left by section 2, where the *advertised endpoint* gained cross-host trust but the *discovery
redirect* that fetches the profile did not, so platform-redirecting merchants never reached the new
endpoint logic. A separate gap (no REST transport) is recorded as follow-up.

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

## 3. Discovery redirect trust (aligning the probe with endpoint trust)

Section 2 widened the trust applied to an *advertised* endpoint inside a fetched profile, but the
**discovery GET** that fetches the profile still followed only **same-origin** redirects (an SSRF
guard from the original discovery work). That left a gap: a Shopify store on a custom domain that
redirects `https://brand.com/.well-known/ucp` to its
`https://brand-prod.myshopify.com/.well-known/ucp` shop host — or a merchant that serves the
well-known path from a sibling subdomain — failed discovery entirely (silent miss → fallback to the
conventional `/api/ucp/mcp` path on the wrong host), so the cross-host endpoint trust in section 2
never got the chance to run.

The discovery redirect guard now mirrors the endpoint-trust predicate exactly.
`discover_merchant_ucp_profile` accepts `trusted_suffixes` (callers pass
`ucp_config.trusted_endpoint_suffixes`), and each redirect hop is followed only when it is
**same-origin**, **same-site**, or on a **trusted platform suffix** relative to the *original*
merchant origin — the same `same_origin` / `same_site` / `host_matches_trusted_suffix` checks that
gate the signed POST, not a separate naive host-suffix comparison. Every hop is evaluated against
the original origin (not the previous hop), so a chain cannot walk off the merchant's site one
same-site step at a time. The parsed profile's `origin` stays the original merchant, so advertised
endpoints are still trust-checked relative to the host the user actually chose.

### Security rationale

The discovery GET is unsigned, read-only, and its body is never returned to the model (only a
sanitized "shoppable" hint), so it is strictly *less* sensitive than the signed POST that already
trusts these same hosts. Extending the redirect guard to the same set adds no authority beyond what
section 2 already granted: same-site stays within the merchant's own registrable domain, and trusted
suffixes remain an operator allowlist of public commerce backends that cannot be pointed at the
internal network. With no suffixes configured the behaviour collapses to same-origin/same-site only,
and an untrusted cross-host redirect is still rejected even when an allowlist is configured. The
chain stays bounded by `MAX_DISCOVERY_REDIRECTS` and the shared timeout budget. DNS rebinding
remains out of scope as above.

### `.well-known/ucp.json` discovery fallback

Some static hosts (S3, Netlify, IIS) can't serve the spec's extension-less `/.well-known/ucp` path
without bespoke routing and return `404` for it. When the primary probe returns exactly `404`,
discovery makes **one** secondary attempt against `/.well-known/ucp.json` before treating the
merchant as having no profile. Only a `404` triggers the fallback — a `5xx`, an off-trust redirect,
or a network error is a miss straight away. The fallback request reuses the same trusted-redirect
following and shares the single discovery timeout budget (the `.json` attempt gets only the time
left after the first probe), so it can't double the worst-case discovery latency.

## Follow-up: REST transport (done)

THE ICONIC and Adore Beauty advertise `transport: "rest"`, not `mcp`. The client originally only
spoke the MCP JSON-RPC transport, so those merchants fell back to a non-existent `/api/ucp/mcp` path
and failed (surfacing as `UCP response was not JSON`). REST-transport support has since been added
so those merchants are reachable:

- **Discovery** now surfaces `rest` bindings alongside `mcp` ones
  (`MerchantUCPProfile.rest_endpoints`), and `usable_shopping_endpoint(...)` selects the safest
  endpoint across both transports using the unchanged trust ranking (same-origin → same-site →
  trusted suffix), preferring MCP within a trust class. The resolved transport is threaded through
  `_ResolvedMerchant`.
- **The tools branch on transport.** `_ucp_call` dispatches each UCP operation
  (`create_cart`/`get_cart`/`update_cart`/`create_checkout`) to either the MCP `tools/call` body or
  the REST verb/path from `rest.openapi.json` (`POST /carts`, `GET|PUT /carts/{id}`,
  `POST /checkout-sessions`). The cart/checkout object is the REST request body directly (not
  wrapped under a key) and the resource id is URL-encoded into the path. RFC 9421 signing
  (`sign_ucp_request`) is reused unchanged. A REST response is the cart/checkout object itself,
  re-nested under the MCP `result.structuredContent` shape so
  `_cart_from_response`/`_checkout_from_response` work as-is.
- **Adore Beauty** is checkout-only (advertises checkout but not `dev.ucp.shopping.cart`), so it
  exercises the signed REST checkout handoff; its checkout path requires signing to be configured.
- **Coverage:** `rest.openapi.json` is vendored into `tests/data/ucp/2026-04-08/`, and the contract
  test (`tests/unit/tools/test_shopping_ucp_contract.py`) validates the REST request bodies and
  verb/path against it the same way it validates the MCP `tools/call` bodies.

## Follow-up: capability-only checkout merchants with no `services` block (done)

Modern UCP (2026-04-08) checkout-only merchants publish a profile that advertises only
`capabilities` (e.g. `dev.ucp.shopping.checkout`) and omit the `services` block entirely, often
serving the profile under a path prefix (e.g. `https://host/ucommerce/.well-known/ucp`). Because
there was no binding to parse, `_shopping_endpoints` returned nothing, `supports_shopping` was
`False`, and the client fell through to the (non-existent) Shopify `/api/ucp/mcp` fallback.

- **Parent-base fallback.** `discover_merchant_ucp_profile` derives the API parent directory from
  the _final fetched_ profile URL (`_api_parent_base`: the path ahead of `/.well-known/`, so
  `.../ucommerce/.well-known/ucp` → `.../ucommerce`; a root-hosted profile yields the bare origin)
  and uses it as `endpoint_base`. When the parsed profile has no MCP/REST bindings but advertises
  `dev.ucp.shopping.checkout`, `_parse_merchant_profile` registers `endpoint_base` as the sole
  `rest_endpoints` entry. The synthesized endpoint is same-origin as the merchant so it still passes
  the `usable_shopping_endpoint` trust gate.
- **Subpath-preserving REST routing.** `_rest_route` appends each route template to the base by
  trimming the base's trailing slash and concatenating (not `urljoin`), so a base carrying its own
  path prefix (`https://host/ucommerce`) keeps it — `POST /checkout-sessions` becomes
  `https://host/ucommerce/checkout-sessions`, not `https://host/checkout-sessions`.
- **Coverage:** unit tests in `test_ucp_service.py` cover the parent-base fallback (subpath and
  root-hosted), the checkout-capability gate, and precedence of an explicit binding; the contract
  test adds an end-to-end case resolving a services-less checkout-only merchant to a signed
  `create_checkout` at the subpath-preserved URL.

## Follow-up: subpath discovery and removing the Shopify fallback (done)

Two related changes, motivated by a merchant that serves UCP under a path prefix (e.g.
`https://host/ucommerce/.well-known/ucp`) and by the observation that the Shopify fallback was
turning every discovery miss into a confusing error.

### Preserve the input path prefix during discovery

`discover_merchant_ucp_profile` previously discarded the input URL's path (`merchant_origin(url)`)
and always probed `{origin}/.well-known/ucp`, so a subpath-hosted profile was only reachable if the
root well-known happened to redirect to it. Discovery now **preserves the input path prefix**: it
probes `{origin}{path}/.well-known/ucp` (trailing slash trimmed; an origin-only input still probes
the root). So `https://host/ucommerce` is probed at `https://host/ucommerce/.well-known/ucp`
directly. `endpoint_base` (used both to resolve relative bindings and as the checkout-only REST
fallback) is the parent directory of the _final fetched_ URL, so it tracks any trusted redirect too.
Trust stays anchored to the original merchant origin (`profile.origin`).

Callers are unaffected in practice: the browser probe already passes the bare origin, and the
shopping tools' `business_url` is contractually the merchant base.

### Remove the Shopify `/api/ucp/mcp` fallback (fail fast)

The tools were originally Shopify-only and always hardcoded `{origin}/api/ucp/mcp`; when generalized
to UCP discovery, that path was demoted to a fallback for "Shopify stores that don't publish a
profile." That assumption no longer holds — Shopify publishes a discoverable profile on the
`{shop}.myshopify.com` shop host (a default trusted suffix), reached directly or via the
custom-domain → shop-host redirect discovery already follows. Meanwhile the fallback was actively
harmful: on **any** discovery miss (no profile, 404, network error, undecodable body, untrusted
redirect — all collapsed to `None`) it POSTed to `{origin}/api/ucp/mcp`, which a non-UCP store 404s,
so the model saw `UCP response was not JSON: HTTP 404` from a fabricated URL instead of the real "no
UCP profile" cause.

`_resolve_ucp_endpoint` now **fails fast** with a specific `ValueError` in each case: no profile
found, a profile whose only bindings are untrusted cross-host addresses, or a profile with no usable
binding. Nothing is posted. `SHOPIFY_FALLBACK_MCP_PATH` and the `MCP_TRANSPORT` import are removed.

### Coverage

`test_ucp_service.py` adds a subpath-probe test (asserting `/ucommerce/.well-known/ucp` is fetched)
and a subpath-via-redirect variant. `test_shopping_tools.py` adds a fail-fast-without-profile test
and converts the former cross-host-fallback tests to assert the fail-fast error and that no POST is
made; the remaining flow tests serve a discoverable profile instead of relying on the fallback.
