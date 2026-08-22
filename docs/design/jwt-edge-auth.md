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
 2. **One credential shape past the gateway, two deliveries.** The gateway verifies signatures; it
    cannot consult a database, so opaque bearer tokens cannot be honoured off-LAN. All remote
    clients converge on JWTs; native clients and scripts send them as `Authorization: Bearer`,
    browsers hold them in an HttpOnly cookie set by the session bridge — so browser-managed
    requests that cannot carry headers (`<img>`, `EventSource`, full-page OAuth redirects)
    authenticate automatically instead of each needing its own migration.
 3. **Bootstrap endpoints carry their own auth.** The gateway cannot signature-check requests whose
    whole purpose is obtaining the first credential, plus deliberately public receivers (error
    intake). Those paths form an explicit, enumerated no-JWT route set at the edge; each is
    authenticated by something else (PKCE code, opaque token, session cookie) or is public by
    design with its own abuse controls.

### Backend

1. **Fail-closed middleware**: replace the blanket `^/api(/.*)?$` entry in `PUBLIC_PATHS` with one
   explicit list naming every path that does not use the default API-token/session authentication:
   bootstrap receivers (`/api/auth/exchange`, `/api/auth/refresh`, `/api/auth/token`,
   `/api/auth/browser-token`), public-by-design error intake, and scoped-auth routes (diagnostics,
   whose readonly-token check lives in a route dependency). Every other `/api/*` request requires
   authentication in `AuthMiddleware` — closing the LAN exposure of dependency-less routers as a
   side effect, and making the system safe under any edge configuration. Scoped routes stay scoped:
   passing a diagnostics readonly token grants nothing beyond diagnostics.
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
   cookie, sets the short-lived JWT as an HttpOnly `Secure` `SameSite=Lax` cookie scoped to
   `/api`, and also returns it in the body for non-browser consumers of the session (none today).
   Lax is what lets browser-managed flows survive the gateway unchanged — including returns from
   cross-site redirects such as the Google OAuth callback — while cross-site POSTs (the CSRF case
   that matters) still never carry it.
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

8. After page load, and again when the cookie nears expiry, call the session→JWT bridge; the cookie
   it sets authenticates every subsequent browser-managed API request — decorated `fetch`, native
   `EventSource` subscriptions, `<img>` attachment previews, and full-page OAuth redirects alike.
   No per-request machinery changes. Page loads themselves remain behind Access SSO + session
   cookies — only `/api` traffic changes credential shape. The bridge re-run is driven by the JWT's
   `exp`, which the frontend reads from the bridge response.

### Edge (deployment repo)

9. **Envoy Gateway `SecurityPolicy`** with a JWT provider whose `remoteJWKS` points at the in-cluster
   Family Assistant service, extracting tokens from both the `Authorization: Bearer` header and the
   browser cookie, attached unconditionally to the HTTPRoute section matching `/api/*`, except the
   routes the backend classifies as not using default authentication: bootstrap receivers
   (`exchange`, `refresh`, `token`, `browser-token`), public-by-design error intake, and scoped-auth
   diagnostics routes whose readonly-token check lives in a route dependency. There is no
   header-conditioned splitting and therefore no route an attacker can select by omitting anything;
   every exempted path is authenticated by something else or public by design with its own abuse
   controls. The backend is the single source of truth for that classification — it publishes it,
   and the deployment consumes it (generated config or a contract test), so the two lists cannot
   silently drift.
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
- **Browser cookie JWT and CSRF**: the bridge cookie is `SameSite=Lax`, so it rides top-level GET
  navigations (OAuth callback returns) but never cross-site POSTs — the CSRF case that matters.
  A cross-site top-level GET to `/api` cannot leak data to the initiator (the response renders in
  the victim's browser, unreadable cross-origin).
- **Diagnostics scoped-auth pass-through**: the middleware allowlist names diagnostics paths so
  their readonly-token dependency keeps working; the pass-through grants nothing beyond those
  routes' own scoped checks. This is enumeration at a single chokepoint (the same list drives the
  edge's bootstrap route), not a per-route exemption habit.
- **LAN path enforcement**: LAN/Tailscale traffic enters through the local ingress and may not
  traverse the public gateway, so edge JWT enforcement applies to the internet path; LAN relies on
  backend auth (now actually enforced everywhere, see milestone 1). No posture regression.
- **Embedded web views off-LAN**: WKWebView-hosted pages (e.g. Documents) still hit interactive
  Access when off-Tailscale. Pre-existing behaviour, out of scope here.
- **Single signing key**: one key, stable `kid`; rotation is future work the JWKS shape already
  accommodates.

## Milestones

Each milestone is independently verifiable; M1–M3 land in this repo, M4 in the deployment repo.

1. **Backend fail-closed middleware** — explicit route classification (default-auth vs bootstrap /
   public / scoped), enforced authentication for everything else, published so the edge can consume
   it. Verify: functional tests asserting unauthenticated `/api` requests get 401 (not
   redirect/open), every classified path behaves per its class, previously open read endpoints now
   reject.
2. **JWT tokens + bridges** — ES256 issuance, dual verification, JWKS endpoint, browser bridge
   (cookie + body), opaque→JWT exchange, configuration. Verify: unit tests (mint/verify,
   bad-signature/expiry/audience rejection, JWKS shape, disabled-mode passthrough, cookie attributes)
   and functional `app_auth` tests covering every bootstrap path.
3. **Web frontend** — bridge call on load and near expiry. Verify: frontend test suite; Playwright
   flows (chat streaming, attachment images, activity stream, Google OAuth connect) with the cookie
   present and expired.
4. **iOS** — proportional refresh threshold; HTML/auth-wall detection in response validation and the
   error classifier. Verify: unit tests; full suite green.
5. **Deployment** — Envoy `SecurityPolicy` (header + cookie extraction) + route split generated or
   contract-tested against the backend's published classification, Access bypass rule in tofu.
   Verify: `tofu plan` diff review; contract test fails when classification and policy diverge;
   curl through the tunnel — no token ⇒ 401 from the gateway, valid token (header and cookie) ⇒
   200, opaque token ⇒ 401 with clear pointer to the upgrade endpoint, each classified path
   reachable per its class; web UI unaffected.
6. **End-to-end** — device off-Tailscale: sign-in once, uploads/chat/SSE all work; failure modes
   surface as readable errors.
