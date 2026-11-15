# ChatGPT plan usage ("Sign in with ChatGPT")

## Goal

Let a deployment run OpenAI models on a household member's ChatGPT Plus/Pro plan instead of an API
key, so routine inference draws on the subscription's allowance rather than per-token billing.

## Which mechanism

There are two ways to spend a ChatGPT plan from outside ChatGPT:

1. **OpenAI's Sign in with ChatGPT plan usage** (<https://developers.openai.com/siwc/>). A public
   OAuth client registers dynamically, the user grants `chatgpt.tokens.use.direct`, and the access
   token is a bearer credential for the public `POST https://api.openai.com/v1/responses`. OpenAI
   offers it to open-source and locally hosted apps, which describes Family Assistant: the
   repository is public and every deployment is self-hosted.
2. **Impersonating the Codex CLI**: its client id, the `chatgpt.com/backend-api/codex` endpoint, its
   `originator` header. It works today, it is not sanctioned for other clients, and it can break
   without notice.

We use (1). It is sanctioned, it reaches the API surface the OpenAI client already speaks, and
OpenAI documents its errors and limits.

## Shape

### One plan per deployment

The deployment holds at most one active ChatGPT sign-in. Every model configured with
`provider: chatgpt` uses it, whoever the acting user is. That includes scheduled automations, which
have no acting user. It matches how the deployment's API keys already work, and a household shares
one plan. Per-user plans would mean resolving credentials per turn and failing automations, which
nobody has asked for.

Any authenticated user may sign in or sign out. The app has no admin role, and these are household
members.

### Sign-in from a remote server

The flow only accepts an HTTP loopback redirect on `127.0.0.1`, which reaches the user's browser
machine, not the server. OpenAI's guidance for a self-hosted VM is to finish OAuth locally and copy
the credential file across. We get the same result without a local tool:

1. In Settings → Connected Accounts, the server generates `state`, `nonce` and the PKCE verifier,
   and returns an authorize URL whose `redirect_uri` is `http://127.0.0.1:1455/auth/callback`.
2. The user approves in their browser. The browser is then sent to the loopback URL, which fails to
   load because nothing listens there. The code, state and issued client id are in its address bar.
3. The user pastes that URL into the settings page. The server checks `state`, exchanges the code
   with the verifier, and validates the ID token: issuer, audience, expiry and `nonce`. The ID token
   arrives directly from the token endpoint over TLS, which OIDC Core §3.1.3.7 allows in place of a
   signature check. The server also checks that the granted scopes include
   `chatgpt.tokens.use.direct`.

Pending attempts are held in memory, one per user, for ten minutes. The app runs as one process, and
a restart only means starting the sign-in again.

The host identifier (`ext_agent_host_id`) is generated once per deployment and kept across sign-out.
The issued client id, subject and email are kept too, so signing in again uses the documented
returning-account flow (`id_token_hint`, `login_hint`, no new registration). Signing in with a
different account registers a new client.

### Credentials

`chatgpt_plan_credentials` holds the single registration. The access, refresh and ID tokens are
stored as Fernet ciphertext under the deployment's existing `CREDENTIAL_ENCRYPTION_KEY`. The row
also keeps the access-token expiry, `earliest_refresh_at`, the granted scopes, a status (`active`,
`needs_reauth`, `signed_out`) and a generation that rotates on every credential write.

Refresh tokens rotate on every use, and a reused one is rejected. So:

- One in-process lock serializes refreshes.
- The rotated set is written with a compare-and-swap on the generation it was read under, so a
  concurrent sign-in is never overwritten by a stale refresh.
- The access token is persisted too. A restart then does not spend a refresh, and every refresh is a
  rotation that could be lost.

A terminal refresh error, or `invalid_grant`, sets the status to `needs_reauth`. The terminal codes
are `invalid_refresh_token`, `token_expired`, `refresh_token_expired`, `refresh_token_invalidated`
and `refresh_token_reused`. Transient failures change nothing. Sign-out revokes the refresh token at
the published revocation endpoint (best effort), then clears the tokens.

### Inference

`provider: chatgpt` selects `ChatGPTPlanClient`, a subclass of `OpenAIClient` that is always on the
Responses API. Its SDK client takes its bearer token from the credential store before each request.
The route has some extra requirements:

| Route requirement                                             | How the client meets it                                                                                  |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `store: false`, `stream: true` on every request               | Forced. Non-streaming calls are streamed and collected through `response.completed`.                     |
| No `system` items                                             | System messages are sent as `developer` messages.                                                        |
| No `max_output_tokens`, `temperature`, `top_p`, `metadata`, … | A configured unsupported parameter is a startup error.                                                   |
| No Chat Completions                                           | Structured and JSON output use Responses `text.format`.                                                  |
| Usage-limit errors (`subscription_sharing_*`)                 | Mapped to the typed errors: limits to `RateLimitError`, eligibility to `AuthenticationError`, and so on. |

Not being signed in raises `AuthenticationError` with a message pointing at Settings. A
`retry_config` whose fallback is an API-key model therefore keeps a profile working while the plan
is exhausted or signed out. This is the recommended configuration.

Models that only exist on the plan side are named as usual, for example
`{model: gpt-6.1-sol, provider: chatgpt}`.

## Deliberate simplifications

- **No keep-alive refresh.** A refresh token lapses after 30 days unused. A deployment that leaves a
  plan model idle that long has to sign in again, which the settings page and the error message both
  say.
- **No model picker.** Operators name models in configuration as they do for every provider. A model
  the plan cannot use fails on its first request with OpenAI's own error.
- **Cache controls are unchanged.** `additional_tools` is documented as supported on this route. The
  client keeps the same cache-control behaviour as direct OpenAI.
- **The paste step instead of a listener.** It costs the user one copy and paste, and needs no local
  tool.

## Verification

- Unit tests for the credential store: refresh, rotation compare-and-swap, terminal versus transient
  refresh failures, and sign-out revocation, against an `httpx.MockTransport`.
- Unit tests for the paste-URL flow: state mismatch, a missing issued client id, a missing plan
  scope, and nonce or audience mismatch.
- Unit tests for the request shape the client sends, and for streaming collection on the
  non-streaming paths.
- A frontend test for the settings card.
- A live end-to-end run needs a real ChatGPT account. It is not automated.
