# MCP adapter: Family Assistant as an MCP server

## Problem

Family Assistant can *consume* MCP servers (`mcp_config.mcpServers`), but nothing lets another agent
consume *it*. The motivating case is a Claude chat on claude.ai, or a Claude Code session, that
wants to ask the household assistant a question ("what's on the calendar this weekend?", "add milk
to the shopping list") and get the answer back into its own conversation. The A2A server already
exposes the assistant to other *agents*, but neither claude.ai nor Claude Code speaks A2A; both
speak MCP over Streamable HTTP.

## Approach

Expose one MCP server, mounted inside the existing FastAPI app, with a single tool:

- `ask_family_assistant(question, conversation_id=None)` runs the question through a processing
  profile as the authenticated user and returns the reply together with the conversation id, so the
  caller can hold a multi-turn conversation by passing the id back.

The turn itself is the existing non-streaming chat path (`POST /api/v1/chat/send_message`),
extracted into a function the REST endpoint and the MCP tool both call. Everything that path already
gets right — conversation ownership, one turn per conversation, model tier resolution, durable
deferred confirmations when a tool needs approval, idempotent turn ids — is inherited rather than
re-implemented. The MCP surface adds transport and authentication, not behaviour.

### Transport

Streamable HTTP at `/api/mcp`, stateless with JSON responses: every request is self-contained, so
there are no sessions to make sticky and no SSE keep-alives to hold open. The endpoint lives under
`/api` on purpose. `AuthMiddleware` is fail-closed for everything under `/api` — a request must
carry a session or a bearer token — so the MCP endpoint is authenticated by the same chokepoint as
the rest of the API rather than by a second scheme of its own.

The mount is the SDK's raw ASGI handler rather than the Starlette app it can wrap it in, so the
request the tool sees still belongs to the outer application and `request.app.state` (processing
services, database engine, config) resolves exactly as it does in a router. The SDK's session
manager must be running for the handler to serve requests; the app lifespan runs it.

### Authentication

Two credentials work, and they are the same credential under the hood:

1. **An ordinary API token** from `/api/me/tokens`, sent as `Authorization: Bearer …`. This is what
   Claude Code (`claude mcp add --header`), the Messages API MCP connector (`authorization_token`),
   and any scripted client use. Nothing new is needed for it.

2. **OAuth 2.1**, which is what claude.ai custom connectors require for per-user sign-in (its
   static-header option is a gated beta). Family Assistant becomes an OAuth authorization server for
   this one purpose: the MCP SDK ships the protocol endpoints
   (`/.well-known/oauth-authorization-server`, `/authorize`, `/token`, `/register`, `/revoke`)
   behind an `OAuthAuthorizationServerProvider` interface; we implement the provider and a consent
   page. The flow Claude runs is dynamic client registration, then authorization code with PKCE
   S256, then token exchange with rotating refresh tokens. `/api/mcp` answers an unauthenticated
   request with `401` and a `WWW-Authenticate` header pointing at protected resource metadata served
   at `/.well-known/oauth-protected-resource/api/mcp`, which is how a client discovers the
   authorization server.

   The `/authorize` handler validates the request and redirects to a consent page. The consent page
   is an ordinary authenticated page: with OIDC configured, `AuthMiddleware` sends a signed-out user
   through the existing login and back. Approving mints an authorization code bound to the signed-in
   user; the token endpoint exchanges it for an access token.

An OAuth access token is a row in `api_tokens` with `token_type = "mcp"` and the OAuth client it was
issued to. That row *is* the grant: a refresh rotates its secret and expiry in place and replaces
only the refresh row, so the entry a user sees on the token page keeps its identity however many
times the client has refreshed, and revoking it disconnects the client. It is verified by the same
code path as an API token, with one difference enforced in `AuthMiddleware`: an `mcp` token
authenticates only requests to the MCP endpoint. A connector that was granted "ask the assistant"
cannot use its token to call the rest of the REST API. Refresh tokens reuse the existing `refresh`
token type and parent link, so revoking the access token cascades. Because the tokens are
`api_tokens` rows, the token-management page lists them alongside the user's other tokens and
revocation works from there with no new UI.

### Trust

The tool runs as the authenticated user, under the profile the operator configures for the adapter
(the default profile unless `mcp_adapter.profile_id` says otherwise; remote delegation-only profiles
are refused). The question text arrives from a machine acting for that user, so the turn starts with
the same taint the A2A endpoints give a peer's message — recognized machine, not direct user input —
and the existing taint policy decides what that means for each tool. In Rule of Two terms the
profile keeps its own properties; the adapter adds no data access and no side effects beyond what
the profile already has.

The whole adapter is off unless `mcp_adapter.enabled` is set. Dynamic client registration is an
unauthenticated write, and an operator who does not use the feature should not carry that surface.

## Deliberate simplifications

- **One tool, one scope.** No profile picker, no attachments, no streaming. A caller that wants a
  different profile is a configuration change (`mcp_adapter.profile_id`), not a tool argument.
- **Authorization codes and pending consents live in process memory**, as the iOS app-auth codes
  already do. They are single-use and expire within minutes; a restart mid-flow means the user
  clicks "connect" again.
- **Dynamic-client secrets are stored as issued.** The SDK's client authenticator compares them in
  clear, and a client secret identifies the *software* (Claude), not a person; the user's authority
  is only ever in the hashed access token. Registered clients are persisted so a restart does not
  invalidate a connector.
- **No Client ID Metadata Documents, no pre-registered clients.** Dynamic registration is what
  claude.ai and Claude Code do out of the box. Because it is an unauthenticated write, it is
  admitted per address at the same rate as public error intake, and the table is capped: past the
  cap, registrations that never produced a live token are pruned oldest first, and a registration is
  refused rather than evicting a working connector.
- **The consent page is server-rendered HTML.** It is one form with two buttons, reached only mid
  OAuth flow, and the OIDC callback precedent already renders server-side; a React route would add a
  frontend build dependency to a protocol handshake.

## Work plan

1. **MCP endpoint with API-token auth.** `mcp_adapter` config, the mounted server, the
   `ask_family_assistant` tool over the shared non-streaming turn function, the taint source, and
   the lifespan wiring. Verified by functional tests that drive the endpoint with the MCP Python
   client against the mock LLM: a question gets the mock reply, a second question with the returned
   conversation id lands in the same conversation, another user's conversation id is refused, a
   request with no credential gets `401`, and the endpoint is `404` when disabled.
2. **OAuth authorization server.** Client storage and migration, the provider, consent page, token
   issuance as `mcp`-typed rows with refresh rotation, the `mcp`-token path restriction in
   `AuthMiddleware`, protected-resource metadata and the `WWW-Authenticate` pointer. Verified by a
   functional test that walks the full flow over HTTP — register, authorize, consent, exchange with
   PKCE, call the tool with the token, refresh, revoke — plus a test that an `mcp` token is rejected
   on a non-MCP API route and a wrong PKCE verifier is rejected.
3. **Documentation.** Operator configuration in `CONFIGURATION_REFERENCE.md`, a user guide page on
   connecting Claude and Claude Code, and the deployment note that `SERVER_URL` must be the public
   HTTPS origin because it is the OAuth issuer and the resource identifier.
