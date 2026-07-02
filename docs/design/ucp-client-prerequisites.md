# UCP Client Prerequisites

## Context

Universal Commerce Protocol (UCP) support needs two platform prerequisites before higher-level
shopping workflows can be built:

- a public platform profile at `/.well-known/ucp`
- request signing support for merchant-facing UCP calls

The current core user journey is: find a product online, build a cart, and send the buyer a merchant
checkout link. Payment completion remains outside the assistant.

## Design

The application exposes a public `GET /.well-known/ucp` endpoint that serializes the configured
platform profile. The profile advertises UCP version `2026-04-08`, default shopping service and
capability declarations, optional payment handlers, and public signing keys when signing is
configured. Private key material stays in config/env/file inputs and is never published.

Signed UCP requests are built by a service module that:

- loads EC P-256 or P-384 private keys from `UCP_SIGNING_PRIVATE_KEY` or
  `UCP_SIGNING_PRIVATE_KEY_PATH`
- publishes the matching public JWK in the platform profile when `UCP_SIGNING_KEY_ID` is set
- signs requests with RFC 9421 HTTP Message Signatures using ES256 or ES384
- adds `UCP-Agent`, `Signature-Input`, `Signature`, `Content-Digest`, and state-changing
  `Idempotency-Key` headers as required

The Shopping skill activates scoped UCP tools in the default assistant profile:

- `ucp_add_to_cart`
- `ucp_get_cart`
- `ucp_transfer_checkout_to_human`

These tools call the merchant's JSON-RPC MCP endpoint (discovered from the merchant profile, see
[Merchant Compatibility](#merchant-compatibility-any-ucp-merchant-not-just-shopify) below) with
`meta["ucp-agent"].profile`. Cart calls can run without request signing for estimate/link workflows.
Checkout handoff requires signed UCP requests because merchant Checkout MCP endpoints require
authentication or a signed request. No tool calls `complete_checkout`, submits payment, or accepts a
payment instrument.

## Type Shape

The protocol-defined profile structure uses explicit Pydantic models. UCP capability and service
`config` objects remain extensible because those shapes are defined by individual UCP service and
capability schemas, not by the core platform profile. They are represented as named Pydantic
extension models rather than raw JSON aliases.

## Merchant Compatibility (any UCP merchant, not just Shopify)

The original shopping tools were hardcoded to Shopify: the MCP endpoint was always
`https://{host}/api/ucp/mcp` and the tools were named `shopify_*`. UCP itself is merchant-neutral,
so the tools are generalized to work against any merchant that advertises a UCP profile, while still
working with Shopify stores.

### Merchant discovery

UCP merchants advertise their own profile at `https://{host}/.well-known/ucp`, the same convention
this application uses to publish its platform profile. That profile lists the merchant's service
bindings, each with a `transport` and an `endpoint`. The shopping MCP endpoint is therefore
discoverable rather than assumed.

`services/ucp.py` gains `discover_merchant_ucp_profile(origin, *, client)`:

- issues `GET {origin}/.well-known/ucp`
- parses the `ucp.services["dev.ucp.shopping"]` bindings and returns the first binding whose
  `transport` is `mcp` together with its (absolute, HTTPS) `endpoint`
- also surfaces the advertised service and capability names and the profile `version`
- returns `None` on any failure (non-HTTPS origin, network error, non-2xx, non-JSON, no shopping MCP
  binding) so callers can fall back cleanly

A relative `endpoint` is resolved against the origin; a non-HTTPS endpoint is rejected.

### Endpoint resolution (discovery only, fail fast)

> **Update:** The UCP tools originally fell back to the Shopify convention (`{origin}/api/ucp/mcp`)
> when discovery found no usable binding. That fallback has been **removed** — see
> [UCP discovery: subpath probing and no Shopify fallback](ucp-cart-response-and-cross-origin-endpoints.md#follow-up-subpath-discovery-and-removing-the-shopify-fallback-done)
> for the rationale. The tools now resolve the endpoint purely by discovery and **fail fast** with a
> clear error when no usable binding is found. Genuine Shopify stores are unaffected: they publish a
> discoverable profile on their `*.myshopify.com` shop host (a default trusted suffix), reached
> directly or via the custom-domain redirect discovery follows.

The UCP tools resolve the merchant endpoint from its `/.well-known/ucp` profile. Discovery reuses
the same `httpx.AsyncClient` that performs the subsequent signed/unsigned tool POST.

Two safeguards apply to the discovered endpoint because the profile is untrusted merchant-controlled
metadata:

- **Trusted hosts only.** Every advertised binding is considered, and the first one that is
  same-origin, same-site, or on a configured trusted platform suffix (relative to the
  `business_url`) is selected. A cross-host endpoint that matches none of these is refused — and,
  with no fallback to guess, the tool raises a clear error rather than posting anywhere — so a
  malicious profile cannot redirect the signed POST at an arbitrary or internal host (SSRF).
- **Bounded discovery time.** The discovery GET has its own short timeout (independent of the longer
  MCP POST timeout), so a store that does not publish a profile — and may tarpit the well-known path
  — fails promptly instead of stalling each call.

### Tool rename

The three tools are renamed to merchant-neutral names, reflecting that they work with any UCP
merchant:

- `shopify_add_to_cart` -> `ucp_add_to_cart`
- `shopify_get_cart` -> `ucp_get_cart`
- `shopify_transfer_checkout_to_human` -> `ucp_transfer_checkout_to_human`

The JSON-RPC method names (`create_cart`, `get_cart`, `update_cart`, `create_checkout`) are
UCP-standard and unchanged. Following the project's no-backwards-compatibility-for-internal-code
policy, the old names are removed outright; the skill, tool policy, metadata, and tests are updated
in lockstep.

### Browser auto-detection

So the assistant knows a site is shoppable while browsing, UCP detection is folded into the shared
snapshot path used by every snapshot-returning browser tool (`browser_open`, `browser_snapshot`,
`browser_click`, `browser_fill`, `browser_select`, `browser_wait`). After the snapshot, the current
origin's `/.well-known/ucp` is probed (HTTPS origins only) using the same discovery service — but
only when the origin **changed** since the previous snapshot, so the common flow of opening a search
page and clicking through to a merchant surfaces the hint exactly once on arrival rather than
repeating it on every action against the same page. When a shopping-capable profile is found, a
short hint line is appended to the accessibility snapshot the model reads, naming the advertised
capabilities and the `business_url` to pass to the UCP tools. Capability names come from the
untrusted merchant profile, so only well-formed suffixes are echoed (and the list is bounded) to
keep merchant-controlled text from injecting instructions into the assistant-authored hint. Probe
results (including negative results) are cached per browser session keyed by origin, so revisiting
an origin costs at most one extra request.

Because the probe issues an outbound request from the Family Assistant process (not from the browser
sandbox), it is deliberately constrained to a read-only `GET` of a fixed, reserved metadata path
(`/.well-known/ucp`) over HTTPS, and the response is never returned to the page — only a sanitized
"shoppable" hint reaches the model. Those properties (fixed read-only path, HTTPS-only, no response
exfiltration) mean a browsed page cannot turn the probe into a meaningful SSRF primitive — it cannot
reach plaintext metadata services, change state on a conventional well-known path, or read an
internal response back — so no private-address blocklist is applied to it. The higher-severity
control lives on the shopping POST path instead, where requests are signed and
same-origin-restricted (see above).

The probe only *informs* the model. Browsed pages and their `/.well-known/ucp` profiles are
untrusted external content under the Rule of Two: detection adds no new authority. Checkout remains
a human handoff and still requires a signed UCP request, exactly as before — no tool completes
checkout or submits payment.

## Follow-Up Work

Follow-up work should add richer product discovery against merchant catalog APIs, order tracking,
and merchant-specific response validation, plus surfacing discovered capabilities (beyond cart and
checkout) as the merchant ecosystem grows.
