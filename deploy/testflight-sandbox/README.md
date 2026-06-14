# TestFlight / App Review Sandbox

A **separate, self-contained deployment** of Family Assistant for handing to Apple (TestFlight Beta
App Review or App Store review) without exposing your real data or letting anyone on the internet
use your LLM API key.

It bundles a tiny [Dex](https://dexidp.io/) OIDC provider so the deployment can require a real login
**with zero external identity-provider signup** — Dex owns a single demo account whose credentials
you control and hand to Apple.

## Why this is safe

| Concern                                    | How it's handled                                                                                                                                                                                       |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Private data exposure                      | Brand-new, empty database. Your family's data never touches this instance.                                                                                                                             |
| Random internet users burning your API key | Every tool/LLM call sits behind OIDC auth. `ALLOWED_OIDC_EMAILS` restricts login to the one demo account — enforced for both the web session (`auth.py`) and the native iOS PKCE flow (`app_auth.py`). |
| A leaked demo credential running up a bill | Use a **dedicated, quota-capped OpenRouter key** with a hard credit limit. The whole sandbox runs on the cheap `deepseek/deepseek-v4-flash` model, and `config.yaml` caps `max_iterations`.                |
| Untrusted-input attack surface             | Telegram, email intake, Home Assistant, calendars, and push are all left unconfigured (disabled), leaving only the web + iOS surfaces a reviewer needs.                                                |

> Auth note: this sandbox deliberately does **not** set a `users:` mapping. With no users
> configured, Family Assistant uses the OIDC email/subject directly as the identity, so the email
> allowlist is the only gate you need to manage.

## Models (one cheap key for everything)

Both the LLM and embeddings run through **OpenRouter's OpenAI-compatible API** — i.e. the app's
`openai` provider pointed at `https://openrouter.ai/api/v1` via `OPENAI_BASE_URL`. A single
quota-capped OpenRouter key powers both:

| Use        | Model                            | Why                                                                                                              |
| ---------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Chat       | `deepseek/deepseek-v4-flash`     | Cheapest output ($0.18/1M) among reputable tool-calling models, with the best agentic-tool-use benchmarks in its tier — reliable function calling is what the demo needs. |
| Embeddings | `openai/text-embedding-3-small`  | 1536-dim, cheap, served by OpenRouter on the same key.                                                           |

`config.yaml` pins **every** service profile to the chat model with `provider: openai`, so even the
research / complex-tasks / automation profiles route to the one cheap model instead of their
default Gemini/Claude/GPT pins.

> **Dependency:** the OpenAI-compatible embedding provider comes from PR #909
> (`feat/openai-compatible-embeddings`). This sandbox must be deployed from a branch that includes
> it (it is already merged into this branch's base).

## What's in this folder

| File                  | Purpose                                                  |
| --------------------- | -------------------------------------------------------- |
| `render.yaml`         | Render Blueprint: Postgres + app + Dex, wired together.  |
| `docker-compose.yaml` | Self-hosted (VPS / local) variant of the same stack.     |
| `.env.example`        | Template for the compose variant. Copy to `.env`.        |
| `config.yaml`         | Family Assistant overrides (cost cap, integrations off). |
| `dex/config.yaml`     | Dex config — one static demo user, one OIDC client.      |
| `dex/Dockerfile`      | Official Dex image with the config baked in.             |

## Prerequisites (both paths)

1. **A dedicated, quota-capped OpenRouter key.** Create a key at
   <https://openrouter.ai/settings/keys> and set a hard credit limit on it. This single key powers
   both chat and embeddings (via OpenRouter's OpenAI-compatible API). Never reuse your personal key
   here.
2. **A demo password hash.** Pick a demo password and bcrypt it:
   ```bash
   htpasswd -bnBC 10 "" 'your-demo-password' | tr -d ':\n'
   ```
   (`htpasswd` ships with `apache2-utils` / `httpd-tools`.)
3. A demo email address, e.g. `appreview-demo@example.com`. It does **not** need to be a real,
   deliverable mailbox — Dex just checks the password — but pick something sensible to show the
   reviewer.

______________________________________________________________________

## Path A — Render (recommended)

Render reads `render.yaml` from the repo root by default, so either point the Blueprint instance at
this file's path, or copy it to the repo root on a dedicated sandbox branch.

### First deploy

1. Create the Blueprint from `deploy/testflight-sandbox/render.yaml`. Render provisions the database
   and both web services and prompts for the `sync: false` values — you can leave the three URL
   values blank for now.

2. Once the services are up, note their assigned public URLs, e.g. `https://fa-sandbox.onrender.com`
   and `https://fa-sandbox-dex.onrender.com`.

3. Set the URL env vars that couldn't be known until now, then redeploy both services:

   | Service          | Variable              | Value                                                                  |
   | ---------------- | --------------------- | ---------------------------------------------------------------------- |
   | `fa-sandbox-dex` | `DEX_ISSUER`          | `https://fa-sandbox-dex.onrender.com`                                  |
   | `fa-sandbox-dex` | `APP_BASE_URL`        | `https://fa-sandbox.onrender.com`                                      |
   | `fa-sandbox-dex` | `DEMO_EMAIL`          | `appreview-demo@example.com`                                           |
   | `fa-sandbox-dex` | `DEMO_PASSWORD_HASH`  | *(bcrypt hash from above)*                                             |
   | `fa-sandbox`     | `OIDC_DISCOVERY_URL`  | `https://fa-sandbox-dex.onrender.com/.well-known/openid-configuration` |
   | `fa-sandbox`     | `ALLOWED_OIDC_EMAILS` | `appreview-demo@example.com`                                           |
   | `fa-sandbox`     | `OPENAI_API_KEY`      | *(your capped OpenRouter key)*                                         |

   `OIDC_CLIENT_SECRET` is generated on the app and copied to Dex automatically;
   `SESSION_SECRET_KEY` is auto-generated. You don't set either by hand.

4. Visit `https://fa-sandbox.onrender.com`, log in with the demo email + password, and confirm it
   works end to end.

> **Cold starts:** Render's `starter` instances sleep when idle and take ~30–60s to wake. That can
> make the reviewer's *first* request look broken. Warm both services right before you submit, or
> bump to a plan that doesn't idle.

______________________________________________________________________

## Path B — Self-hosted (docker-compose)

For a VPS you already run behind a reverse proxy with TLS.

1. `cp .env.example .env` and fill every value. `APP_BASE_URL` and `DEX_ISSUER` must be the **public
   HTTPS URLs** your reverse proxy serves (see the reachability note at the top of
   `docker-compose.yaml`).
2. Point your reverse proxy at the published host ports `127.0.0.1:8000` (app) and
   `127.0.0.1:5556` (dex). (Only a proxy running *inside* the compose network would
   use the service names `app:8000` / `dex:5556`.)
3. Bring it up:
   ```bash
   docker compose --env-file .env up --build -d
   ```

______________________________________________________________________

## Pointing the app at the sandbox

The iOS app authenticates against whatever server URL it's configured with, and the OIDC redirect
URIs Dex accepts are `<APP_BASE_URL>/auth` (web) and `<APP_BASE_URL>/app-auth-callback` (native).
Make sure the TestFlight build either targets the sandbox host or lets the tester enter it — and
tell Apple which URL to use in the review notes if it's user-entered.

## What to give Apple

In App Store Connect → your build → **Beta App Review Information** (or App Review Information),
provide:

- **Sign-in required:** Yes
- **Username:** the demo email
- **Password:** the demo password (the plaintext you hashed)
- **Notes:** the sandbox server URL, if the app asks the tester to enter one.

## Tearing it down

Delete the Render Blueprint (or `docker compose down -v`) when review is complete. Because the
database was never shared with production, nothing of yours is left behind. Revoke the capped
OpenRouter key for good measure.
