# JWT-Based Edge Authentication for Native Clients

Status: proposed (revised after review)
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

Make every authenticated client of `/api` speak **standard signed JWTs**, publish them via a **JWKS
endpoint**, have the **Envoy Gateway** (already in the request path) require them unconditionally on
`/api/*`, and give Cloudflare Access a bypass scoped strictly to `/api/*`. Nothing in Family Assistant
or its clients becomes specific to this deployment's Cloudflare setup.

```
any client ──Bearer JWT──▶ Cloudflare (bypass for /api/* only) ──▶ Envoy Gateway
                                                                    ├─ /api/auth/exchange, /api/auth/refresh: no JWT (bootstrap)
                                                                    └─ all other /api/*: valid JWT required — no exceptions
main host, everything outside /api: unchanged (Access SSO; web session cookies for pages)
LAN/Tailscale path: enters at the local ingress; backend enforces auth as below regardless
```

The review of this design established two constraints that shape everything else:

1. **Fail closed, everywhere.** An earlier draft conditioned edge JWT validation on the presence of
   an `Authorization` header, leaving header-less requests on an unprotected route once Access was
   bypassed — and several API routers declare no auth dependency today because `PUBLIC_PATHS`
   blanket-bypasses `/api` and Access was de facto the gate. The fix is a chokepoint, not routing:
   the backend itself must enforce authentication on every `/api/*` request.
2. **One credential shape past the gateway.** The gateway verifies signatures; it cannot consult a
   database, so opaque bearer tokens cannot be honoured off-LAN. Rather than carve out routes per
   credential type (which reopens 1), all remote clients converge on JWTs.

### Backend

1. **Fail-closed middleware**: replace the blanket `^/api(/.*)?$` entry in `PUBLIC_PATHS` with an
   explicit allowlist of genuinely public API endpoints (`/api/auth/exchange`,
   `/api/auth/refresh`, error intake). Every other `/api/*` request requires authentication in
   `AuthMiddleware` — closing the LAN exposure of dependency-less routers as a side effect, and
   making the system safe under any edge configuration. Endpoints with deliberate public receivers
   name themselves on the allowlist rather than relying on the blanket bypass.
2. **Token issuance** (`exchange`, `refresh`): return `api_token` as a short-lived ES256-signed JWT
   instead of an opaque secret. Claims: `iss`, `aud=family-assistant-api`, `sub` (user), `tid`
   (token row id), `jti`, `iat`, `exp` (default TTL 1 hour, configurable). The refresh flow is
   mechanically unchanged — new token row, relink, new JWT — and the opaque refresh token stays
   opaque (the gateway never sees it).
3. **Dual verification**: `get_current_user` accepts Bearer credentials in both formats — JWTs
   (verify signature/expiry/issuer/audience, then load the token row by `tid` for revocation and
   expiry checks) and legacy opaque tokens (unchanged path). Opaque tokens remain valid wherever the
   gateway is not in the path (LAN/Tailscale); off-LAN they are rejected by design.
4. **Session→JWT bridge**: `GET /api/auth/browser-token`, authenticated by the existing OIDC session
   cookie, returns a short-lived JWT of the same shape. This is how the web frontend crosses the
   gateway.
5. **Opaque→JWT exchange**: `POST /api/auth/token` upgrades a valid opaque API token to a JWT pair,
   so scripts using the documented token interface keep working remotely after a one-time upgrade;
   the opaque token stays valid on LAN.
6. **JWKS endpoint**: `GET /.well-known/jwks.json`, unauthenticated, `Cache-Control` friendly to the
   gateway's fetch interval. Public key derived from the configured private key; served as a key list
   with a stable `kid` so rotation can be added without format change.
7. **Configuration**: signing key supplied as a PEM private key via configuration/environment
   (documented in `CONFIGURATION_REFERENCE.md`). Unset ⇒ feature disabled: issuance stays opaque,
   middleware enforcement still applies (it is independent of signing), and the deployment simply
   does not enable the gateway policy or Access bypass.

### Web frontend

8. On load (and again when the stored token nears expiry), fetch a bridge JWT from the session and
   attach it as `Authorization: Bearer` to API calls, including fetch-based streaming, which already
   dominates. The one native `EventSource` subscription (activity stream) converts to fetch-based
   SSE so it can carry the header. Page loads themselves remain behind Access SSO + session cookies —
   only `/api` traffic changes shape.

### Edge (deployment repo)

9. **Envoy Gateway `SecurityPolicy`** with a JWT provider whose `remoteJWKS` points at the in-cluster
   Family Assistant service, attached unconditionally to the HTTPRoute section matching `/api/*`,
   except a small bootstrap route for the two auth endpoints. There is no header-conditioned
   splitting and therefore no route an attacker can select by omitting anything.
10. **Cloudflare Access bypass** scoped strictly to `assistant.andrewgarrett.dev/api/*`, ordered
    ahead of the wildcard app. Nothing else changes: the web UI, static assets, and pages remain
    behind interactive Access SSO, and `/api/*` remains behind two layers anyway (Cloudflare
    WAF/tunnel plus gateway JWT validation).

### iOS

11. **Refresh cadence**: the client already stores `expires_in` and refreshes near expiry through a
    coalesced single-flight path; the freshness threshold becomes proportional to token lifetime
    instead of the current fixed "more than an hour left" check, which would otherwise fire on nearly
    every call once tokens live for an hour. No protocol change.
12. **Error surfacing**: response validation rejects `text/html` responses and redirect-to-login
    chains before decoding, classified into the error model as an explicit condition ("server
    requires sign-in" / connectivity), never surfaced raw as a decode failure.

## Deliberate simplifications

- **Edge revocation lag**: a revoked token is rejected immediately by the backend but admitted by the
  gateway until `exp` (≤ TTL). Standard OAuth2 semantics; the gateway gate keeps unauthenticated
  traffic out, while authorization and revocation stay with the backend.
- **Off-LAN opaque tokens end**: scripts holding manually created tokens must call the upgrade
  exchange once to work remotely; LAN use is unaffected. Documented in the API README.
- **LAN path enforcement**: LAN/Tailscale traffic enters through the local ingress and may not
  traverse the public gateway, so edge JWT enforcement applies to the internet path; LAN relies on
  backend auth (now actually enforced everywhere, see milestone 1). No posture regression.
- **Embedded web views off-LAN**: WKWebView-hosted pages (e.g. Documents) still hit interactive
  Access when off-Tailscale. Pre-existing behaviour, out of scope here.
- **Single signing key**: one key, stable `kid`; rotation is future work the JWKS shape already
  accommodates.

## Milestones

Each milestone is independently verifiable; M1–M3 land in this repo, M4 in the deployment repo.

1. **Backend fail-closed middleware** — explicit `/api` public allowlist, enforced authentication for
   everything else. Verify: functional tests asserting unauthenticated `/api` requests get 401 (not
   redirect/open), allowlisted endpoints stay reachable, previously open read endpoints now reject.
2. **JWT tokens + bridges** — ES256 issuance, dual verification, JWKS endpoint, browser bridge,
   opaque→JWT exchange, configuration. Verify: unit tests (mint/verify, bad-signature/expiry/
   audience rejection, JWKS shape, disabled-mode passthrough) and functional `app_auth` tests.
3. **Web frontend** — bridge-token adoption across API calls and streaming; EventSource conversion.
   Verify: frontend test suite; Playwright flows against a JWT-enforcing stub where practical.
4. **iOS** — proportional refresh threshold; HTML/auth-wall detection in response validation and the
   error classifier. Verify: unit tests; full suite green.
5. **Deployment** — Envoy `SecurityPolicy` + bootstrap route split, Access bypass rule in tofu.
   Verify: `tofu plan` diff review; curl through the tunnel — no token ⇒ 401 from the gateway, valid
   token ⇒ 200, opaque token ⇒ 401 with clear pointer to the upgrade endpoint; web UI unaffected.
6. **End-to-end** — device off-Tailscale: sign-in once, uploads/chat/SSE all work; failure modes
   surface as readable errors.
