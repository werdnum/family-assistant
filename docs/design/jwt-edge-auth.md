# JWT-Based Edge Authentication for Native Clients

Status: proposed
Date: 2026-08-22

## Problem

The iOS app reaches `assistant.andrewgarrett.dev` through two different front doors: on LAN/Tailscale,
split-horizon DNS resolves directly to the local ingress; off-LAN, public DNS resolves to Cloudflare's
edge, where a wildcard Cloudflare Access app (`*.andrewgarrett.dev`) intercepts every request.

Cloudflare Access has no credential that a native app can hold: its non-browser options are service
tokens (a static shared secret) and mTLS certificates, and it cannot accept an externally minted OIDC
JWT as a session. So off-LAN requests get a 302 to the Access login page, `URLSession` transparently
follows it, and the app receives `200 text/html` where it expected JSON. The decoder throws and the
user sees Foundation's rendering of the `DecodingError` — "Data not in expected format" — which looks
like a corrupt file rather than what it is: an authentication wall.

Meanwhile the app itself already performs real authentication: OIDC PKCE against the same identity
provider Family Assistant uses for the web, exchanging the result for Bearer API tokens. That
credential chain is exactly what an edge should be able to verify — if the tokens were verifiable.

## Approach

Make the app's existing OAuth2-style token flow produce **standard signed JWTs**, publish them via a
**JWKS endpoint**, and have the **Envoy Gateway** (already in the request path) validate them for
`/api/*`. Cloudflare Access keeps guarding everything else; `/api/*` gets a scoped bypass because the
gateway now enforces equivalent identity checks there. The app's wire protocol does not change — it
still sends `Authorization: Bearer` — so nothing in Family Assistant or the app becomes specific to
this deployment's Cloudflare setup.

```
iOS ──Bearer JWT──▶ Cloudflare (bypass for /api/* only) ──▶ Envoy Gateway
                                                            ├─ /api/* (+Authorization header): require valid JWT (JWKS from FA)
                                                            ├─ /api/auth/exchange, /api/auth/refresh: no JWT (bootstrap)
                                                            └─ everything else: untouched (web session cookies; Access SSO upstream)
```

### Backend

1. **Token issuance** (`exchange`, `refresh`): when enabled, return `api_token` as a short-lived
   ES256-signed JWT instead of an opaque secret. Claims: `iss`, `aud=family-assistant-api`,
   `sub` (user), `tid` (api-token row id), `jti`, `iat`, `exp` (default TTL 1 hour, configurable).
   The refresh flow is mechanically unchanged — new token row, relink, new JWT — and the opaque
   refresh token stays opaque (the edge never sees it).
2. **Verification**: `get_current_user` accepts Bearer credentials in both formats — JWTs (verify
   signature/expiry/issuer/audience, then load the token row by `tid` for revocation and expiry
   checks) and legacy opaque tokens (unchanged path). Opaque API tokens are a *public* interface also
   used by scripts and manually created tokens, so dual acceptance is a permanent property of the
   interface, not a migration shim.
3. **JWKS endpoint**: `GET /.well-known/jwks.json`, unauthenticated, `Cache-Control` friendly to the
   gateway's fetch interval. Public key derived from the configured private key; served as a key list
   with a stable `kid` so rotation can be added without format change.
4. **Configuration**: signing key supplied as a PEM private key via configuration/environment
   (documented in `CONFIGURATION_REFERENCE.md`). Unset ⇒ feature disabled and behaviour is exactly
   today's (30-day opaque tokens). Fail fast at startup if the value is set but unparsable.

### Edge (deployment repo)

5. **Envoy Gateway `SecurityPolicy`** with a JWT provider whose `remoteJWKS` points at the in-cluster
   Family Assistant service, attached to an HTTPRoute section matching `/api/*` **and** presence of an
   `Authorization` header. Header-matching splits traffic three ways: web/browser requests (cookies,
   no Authorization header) are unaffected; native requests are JWT-checked at the edge; bootstrap
   endpoints live on their own small route without the policy.
6. **Cloudflare Access bypass** scoped strictly to `assistant.andrewgarrett.dev/api/*`, ordered ahead
   of the wildcard app. Nothing else changes: the web UI, static assets, and pages remain behind
   interactive Access SSO, and `/api/*` remains behind two layers anyway (Cloudflare WAF/tunnel plus
   gateway JWT validation).

### iOS

7. **Refresh cadence**: the client already stores `expires_in` and refreshes near expiry through a
   coalesced single-flight path; the freshness threshold becomes proportional to token lifetime
   instead of the current fixed "more than an hour left" check, which would otherwise fire on nearly
   every call once tokens live for an hour. No protocol change.
8. **Error surfacing**: response validation rejects `text/html` responses and redirect-to-login
   chains before decoding, classified into the error model as an explicit condition ("server requires
   sign-in" / connectivity), never surfaced raw as a decode failure.

## Deliberate simplifications

- **Edge revocation lag**: a revoked token is rejected immediately by the backend but admitted by the
  gateway until `exp` (≤ TTL). Standard OAuth2 semantics; acceptable because the gateway gate exists
  to keep unauthenticated traffic out, while authorization and revocation stay with the backend.
- **LAN path enforcement**: LAN/Tailscale traffic enters through the local ingress and may not
  traverse the public gateway, so edge JWT enforcement applies to the internet path; LAN relies on
  backend auth as today. No posture change.
- **Embedded web views off-LAN**: WKWebView-hosted pages (e.g. Documents) still hit interactive
  Access when off-Tailscale. Pre-existing behaviour, out of scope here.
- **Single signing key**: one key, stable `kid`; rotation is future work the JWKS shape already
  accommodates.

## Milestones

Each milestone is independently verifiable; M1–M2 land in this repo, M3 in the deployment repo.

1. **Backend tokens** — JWT issuance, dual verification, JWKS endpoint, configuration. Verify: unit
   tests (mint/verify, bad-signature/expiry/audience rejection, JWKS shape, disabled-mode passthrough)
   and functional `app_auth` tests updated for the new response shape.
2. **iOS** — proportional refresh threshold; HTML/auth-wall detection in response validation and the
   error classifier. Verify: unit tests for validation and classification; full suite green.
3. **Deployment** — Envoy `SecurityPolicy` + route split, Access bypass rule in tofu. Verify:
   `tofu plan` diff review; curl through the tunnel — no token ⇒ 401 from the gateway, valid token ⇒
   200; web UI unaffected behind Access.
4. **End-to-end** — device off-Tailscale: sign-in once, uploads/chat/SSE all work; airplane-level
   failure modes surface as readable errors.
