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

The deployment holds at most one active ChatGPT sign-in, and every direct-OpenAI chat model uses it,
whoever the acting user is. That includes scheduled automations, which have no acting user. It
matches how the deployment's API keys already work, and a household shares one plan. Per-user plans
would mean resolving credentials per turn and failing automations, which nobody has asked for.

### Signing in is the opt-in

No model configuration names the plan. Once someone signs in, every chat model that calls OpenAI
directly tries the plan first and keeps its API key as the fallback; signing out puts them all back
on the key. Asking each deployment to rewrite its model tiers would make the sign-in do nothing
until an operator also edited configuration, for the same models every time.

The one switch is `chatgpt_plan.enabled` (default on). Off, there is no sign-in in the web app and
no model is routed to the plan. Direct OpenAI means the `openai` provider at OpenAI's own endpoint:
OpenRouter, other compatible endpoints (`base_url`, `OPENAI_BASE_URL`), embeddings and image
generation stay on their keys, because the plan serves none of them.

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

`ChatGPTPlanClient` is a subclass of `OpenAIClient` that is always on the Responses API. Its SDK
client takes its bearer token from the credential store before each request. The route has some
extra requirements:

| Route requirement                                             | How the client meets it                                                                                                 |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `store: false`, `stream: true` on every request               | Forced. Non-streaming calls are streamed and collected through `response.completed`.                                    |
| No `system` items                                             | System messages are sent as `developer` messages.                                                                       |
| No `max_output_tokens`, `temperature`, `top_p`, `metadata`, … | A model configured with one stays on its API key; the plan client is never built for it.                                |
| No Chat Completions                                           | Structured and JSON output use Responses `text.format`.                                                                 |
| Function tools only inside a namespace or `additional_tools`  | Tools are declared in one `family_assistant` namespace; replayed calls name it, and the direct OpenAI client strips it. |
| Usage-limit errors (`subscription_sharing_*`)                 | Mapped to the typed errors: limits to `RateLimitError`, eligibility to `AuthenticationError`, and so on.                |

### Routing

The factory wraps each direct-OpenAI model in `PlanFirstClient`, holding the plan client and the
model's ordinary API-key client:

- Nobody signed in: straight to the API key, with no failed attempt first.
- Signed in: the plan. A failure before anything was produced -- usage limit, refused model or
  parameter, eligibility, a transient error -- is answered by the API key. Once the plan has started
  streaming an answer, a later failure is that answer's, as on any provider.
- A failure that will repeat pauses the plan for that model: an hour for a refusal, fifteen minutes
  for a usage limit, which lifts on the plan's own window. That bounds the wasted attempts to one
  per pause, not one per call.
- No OpenAI key at all: the plan is the only route, and its error is the answer when it fails.

The wrapper sits inside a tier's chain like any client, so the chain's own fallback (another
provider) still follows when both the plan and the key fail. Flex applies to the API-key client
only; the plan has no price class to choose.

## Deliberate simplifications

- **No keep-alive refresh.** A refresh token lapses after 30 days unused. A deployment that leaves a
  plan model idle that long has to sign in again, which the settings page and the error message both
  say.
- **No model catalogue.** Every direct-OpenAI model is tried on the plan; one the plan does not
  serve fails its first attempt, falls back to the key, and pauses for an hour. Reading the
  account's model list would avoid that one attempt at the cost of keeping a catalogue fresh.
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
