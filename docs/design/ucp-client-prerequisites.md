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

The Shopping skill activates scoped Shopify/UCP tools in the default assistant profile:

- `shopify_add_to_cart`
- `shopify_get_cart`
- `shopify_transfer_checkout_to_human`

These tools call Shopify's JSON-RPC MCP endpoint at `https://{shop-domain}/api/ucp/mcp` with
`meta["ucp-agent"].profile`. Cart calls can run without request signing for estimate/link workflows.
Checkout handoff requires signed UCP requests because Shopify's Checkout MCP requires authentication
or a signed request. No tool calls `complete_checkout`, submits payment, or accepts a payment
instrument.

## Type Shape

The protocol-defined profile structure uses explicit Pydantic models. UCP capability and service
`config` objects remain extensible because those shapes are defined by individual UCP service and
capability schemas, not by the core platform profile. They are represented as named Pydantic
extension models rather than raw JSON aliases.

## Follow-Up Work

This change builds the first shopping workflow. Follow-up work should add richer product discovery
against Shopify Catalog APIs, order tracking, and merchant-specific response validation.
