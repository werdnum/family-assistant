# Antigravity Sandbox Environment and Egress Credentials

## Context

[gemini-antigravity-managed-agent.md](gemini-antigravity-managed-agent.md) shipped the `coder`
profile on the Antigravity managed agent and listed **custom environments** as a non-goal: each run
got a fresh default sandbox, and `environment` was left unset. Attachment routing later took half of
that back — `interactions_agent_service.py` mounts delegated attachments as `environment.sources` —
but the other half of the `environment` block, `network`, was still never sent.

`environment.network` is where the Interactions API puts the sandbox's **egress proxy**. It accepts
either the string `"disabled"` (no outbound traffic at all) or an allowlist:

```json
{"allowlist": [
  {"domain": "api.github.com", "transform": [{"Authorization": "Bearer ghs_..."}]},
  {"domain": "*"}
]}
```

Each entry may carry a `transform` list of flat `{header: value}` objects. The proxy injects those
headers into every outbound request matching the domain. That is the mechanism this design is for:
**the sandbox never receives the credential**. The agent issues an unauthenticated request to
`api.github.com`; the proxy adds the `Authorization` header on the way out. Nothing the agent can
print, log, exfiltrate or write to a file contains the token, because the token was never in the
sandbox's address space.

The cluster already runs a GitHub App (`werdnum/family-assistant`'s own app, id `2376485`) whose
private key lives in the `github-app-key` SealedSecret, reflected into the `ml-bot` namespace that
family-assistant runs in. `spawn_worker` pods already receive it via
`ai_worker_config.kubernetes.extra_env`. The Antigravity sandbox is Google-hosted and cannot mount a
Kubernetes secret, so the proxy transform is the only way it reaches GitHub at all.

## Decision

Add an `environment` block to `antigravity_config`, carrying the sandbox's egress policy, and a
credential type that resolves to an injected header at submit time.

```yaml
antigravity_config:
  model: "gemini-3.7-flash"
  environment:
    network: "allowlist"
    allowlist:
      - domain: "*"
      - domain: "github.com"
        credential:
          type: "github_app"
          scheme: "basic"
      - domain: "api.github.com"
        credential:
          type: "github_app"
```

Three design points are load-bearing.

**Credentials are minted per run, not configured.** `type: "github_app"` names a *kind* of
credential, never a value. The resolver reads `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and
`GITHUB_APP_PRIVATE_KEY_PATH` (or `GITHUB_APP_PRIVATE_KEY`) from the environment — the same variable
names the k8s-agent StatefulSet and the ai-worker SandboxTemplate already use — signs a short-lived
RS256 JWT, and exchanges it for an installation access token. The App private key never leaves the
process; only the ~1-hour installation token reaches Google, and only as a proxy transform. A config
file that leaked would disclose which domains get a credential, not the credential.

**A token is minted per run, not held across runs.** The proxy is handed one fixed header and uses
it for the whole of a run, so a run inherits whatever lifetime was left at submit — it cannot be
refreshed once the interaction is created. Caching by "not yet expired" would therefore let a long
run start on a token with minutes to live and lose GitHub partway through, including on a final push
after all the work. Reuse instead spans only the moments one submission takes to resolve its rules
(a config naming `github_app` on both `github.com` and `api.github.com` resolves them within
milliseconds, and both must carry the same token); every new run mints fresh and so gets the longest
window the credential can give it.

That still leaves a ceiling: **a run longer than the token's ~1 hour loses GitHub access partway
through**, and nothing in this design can prevent that, because the header is frozen at submit. The
shipped `max_async_seconds` for `coder` is 7200, twice the token's life. A deployment that wants
GitHub to hold for the whole of every run should lower `max_async_seconds` below an hour on the
credentialed profile; one that prefers long runs should expect the agent to report GitHub failures
late in a very long task. This is a genuine trade rather than a bug to fix here — refreshing would
need the API to accept a credential callback, which it does not.

**`scheme` exists because git and the REST API disagree.** GitHub's REST API takes
`Authorization: Bearer <token>`. Git-over-HTTPS against `github.com` is authenticated as HTTP Basic
with the username `x-access-token` and the token as the password. `scheme: "basic"` renders
`Authorization: Basic base64("x-access-token:<token>")`; the default `scheme: "bearer"` renders
`Authorization: Bearer <token>`. Guessing one and applying it to both domains would fail as a 401 in
the middle of an agent run, which reads as the agent being confused rather than as a config error.

### Rule of Two

The shipped `coder` profile is **[C] only**: it acts (in its sandbox, and via the web) and reaches
no household data. Injecting a GitHub App credential adds **[B]** — the App can read and write the
repositories it is installed on, which are sensitive systems even though they are not the
household's notes. The profile also reads the open web, which is **[A]**.

That is all three on paper. What the three letters do not capture is *how much* [B] — and that is
the axis the mitigation actually runs along here, so it is worth being explicit about each layer.

- **The credential's own scope.** A GitHub App installation token is not an account credential. It
  reaches only the repositories the App is installed on, only with the permissions that installation
  was granted, and it expires in about an hour. Choosing a *scoped* credential over a personal
  access token is the primary mitigation for the [B] this adds: it sets the blast radius before any
  runtime gate is consulted, and it is the layer that still holds if every other one fails. Widening
  the App's installation or its permission grants is therefore a security change of the same weight
  as anything in this document, even though it happens entirely outside this repository.
- **The taint gate.** `taint_sink_class: "sandbox_network"` with `taint_policy.mode: "enforce"`. The
  shipped matrix denies this sink at `unknown_external` and confirms below that, so content the
  assistant derived from an email cannot direct the agent — whether it arrives as request text or as
  a routed attachment. The [A] the profile holds is therefore *the agent's own web reads*, not the
  caller's untrusted input; the caller is gated before a run exists.
- **What the egress policy does and does not add.** A closed allowlist — every reachable domain
  enumerated — would also stop the agent reading an attacker-controlled page mid-run while the proxy
  is still attaching a GitHub credential. An allowlist containing `{"domain": "*"}` does not; that
  shape is what the API documents for "restrict nothing, inject on some", and it is what a coding
  agent that installs packages from arbitrary indexes needs. Which one a deployment picks is a
  risk/benefit call against the scope already set above, not a question with one right answer: the
  residual exposure of allow-all is bounded by what the installation can reach in the first place.

Because this is a deployment decision rather than a property of the software, **`defaults.yaml`
ships no `environment` block at all**. Out of the box `coder` is exactly what it was: an
unrestricted default sandbox with no credentials and no [B]. The credential configuration lives in
the operator's own `config.yaml` (for this cluster, the `family-assistant` ConfigMap in
`kube-config`), where the GitHub App it names also lives.

## Implementation

1. **`config_models.py`** — `AntigravityEnvironmentConfig` (`network`, `allowlist`),
   `AntigravityEgressRuleConfig` (`domain`, `headers`, `credential`) and
   `AntigravityEgressCredentialConfig` (`type`, `header_name`, `scheme`, `token_env`), hung off
   `AntigravityConfig.environment`. Model validators reject the combinations that would otherwise
   fail as silence: an `allowlist` under `network: "default"`/`"disabled"` (built and discarded),
   `network: "allowlist"` with an empty list (which the API reads as "reach nothing" — a run that
   cannot install a package or clone a repo, presented as an agent that just could not manage the
   task), and `type: "bearer"` without `token_env`.
2. **`llm/antigravity_egress.py`** — `AntigravityEgressResolver`, which turns the config into the
   `network` payload and holds the token cache, plus `GitHubAppInstallationTokenSource` doing the
   JWT-then-exchange dance against `api.github.com`. Both take a `Clock`, so expiry is testable
   without sleeping. A missing or malformed credential raises rather than silently omitting the
   header: a run that reaches GitHub unauthenticated fails deep inside the agent with a 404 on a
   private repo, which is the confusing-error-at-another-layer case the error-handling guide is
   about.
3. **`providers/google_genai_client.py`** — `_build_agent_environment()` merges `sources` and
   `network` into one `environment` block and is awaited by both callers, so the egress policy
   applies to the interactive `/coder` path as well as the delegated one. Previously only
   `start_agent_interaction` could set an environment; the stream path had nothing to put there.
4. **`assistant.py`** — `antigravity_environment` joins `antigravity_model` and
   `antigravity_max_total_tokens` in the profile's `client_config`.

## Non-goals

- **Registered environments.** `environment` may also be a string naming an environment created via
  `agents.create()`. Reusing one across runs would mean managing its lifecycle and would break the
  "fresh sandbox per run" property the profile's isolation rests on.
- **Mounting the household's own files.** Still the [B] decision the original design doc deferred.
  `sources` carries delegated attachments and nothing else.
- **Per-turn or per-user credentials.** The App installation is a property of the deployment, so one
  credential serves every run on the profile. Scoping a token to the acting user would need a
  per-user GitHub identity the assistant does not have.
- **Credential kinds beyond `github_app` and `bearer`.** Anything with a static token fits `bearer`
  via `token_env`; a second minted kind can be added when something needs one.
