# Antigravity Stored Egress Credentials

## Context

[antigravity-environment-and-credentials.md](antigravity-environment-and-credentials.md) ships the
sandbox's GitHub access as an **egress proxy header**: at submit time we mint a GitHub App
installation token and hand it to the Interactions API inside
`environment.network.allowlist[].transform`. The proxy injects it on every outbound request to the
matching domain, so the token never enters the sandbox. That property is the reason the design was
acceptable at all, and nothing here gives it up.

What that design could not solve, and said so:

> That still leaves a ceiling: **a run longer than the token's ~1 hour loses GitHub access partway
> through**, and nothing in this design can prevent that, because the header is frozen at submit.
> [...] refreshing would need the API to accept a credential callback, which it does not.

`coder` ships `max_async_seconds: 7200` — twice the token's life. The documented mitigation was to
lower it below an hour on a credentialed profile, trading away long runs to keep GitHub working.

**The API now accepts what that paragraph said it did not.** The
[Agent Credentials API](https://ai.google.dev/gemini-api/docs/agent-credentials), released alongside
`antigravity-preview-09-2026`, stores a credential server-side under an id and lets an allowlist
rule reference it by name:

```json
{"domain": "api.github.com", "credential": "github-production"}
```

The proxy resolves that id **per request** rather than reading a header frozen into the interaction
at submit. The credential's value is therefore no longer a property of the run, and can change while
the run is in flight. That is the whole of the opportunity, and the whole of the risk.

## Decision

Resolve *minted* egress credentials through the credential store rather than through `transform`,
and keep the stored value fresh with a rotation task, so a run's GitHub access outlives the token it
started on. Static credentials stay where they are.

The **profile configuration does not change shape**.
`credential: {type: "github_app", scheme: "basic"}` still names a kind rather than a value, and
still resolves to an installation token minted from the deployment's own App. What changes is where
the minted token goes: into a named credential at Google that outlives the run, instead of into a
header inside it.

Three points carry the design.

**The store holds exactly what must outlive its submit.** That is the minted, expiring credentials —
`github_app` today — and nothing else. A static `bearer` token keeps the `transform` path it uses
now, because freezing a value at submit is only a problem for a value that expires, and a static
token does not. Putting one in the store would buy nothing and cost a secret that persists at Google
with no expiry of its own, where removing its rule from our config would not revoke it.

The dividing line is a property rather than a list: **does this credential need to change while a
run is in flight?** Minted tokens do and are stored; static ones do not and are not. That keeps the
rule decidable at the point a credential kind is added, instead of depending on someone remembering
which mechanism a new kind belongs to.

**Rotation is a clock, not a lifecycle.** A periodic task mints a fresh installation token and
`PATCH`es it over the stored credential. It does not track which runs are live, hold state about
what it last wrote, or coordinate with submit. It is idempotent and safe to run when nothing is
using the credential, which is what keeps it from growing the state machine that `AGENTS.md`'s
review-fix guidance warns about. The cadence is a fraction of the token's ~1h life so that a missed
tick is absorbed rather than fatal.

**Neglect degrades availability, not safety.** If rotation stops, the stored token ages out and
GitHub answers 401 — a visible failure that gets fixed. It does not silently widen anything: a stale
token is a *weaker* credential, never a stronger one, and it cannot outlive GitHub's own expiry
regardless of what our side forgets to do. The dangerous direction — a credential that keeps working
after it should have stopped — is not reachable from here, because GitHub, not this code, decides
when an installation token dies.

### Why the GitHub App does not simply become an `oauth2` credential

The store supports three credential types: `bearer_token` (static), `oauth2`, and
`environment_variable`. Google refreshes only `oauth2`, given `client_id`, `client_secret`,
`refresh_token` and `token_url` — and that is the one type the App flow cannot supply. A GitHub App
**installation** token is not an OAuth2 refresh-token grant; it comes from signing a JWT with the
App's private key and exchanging it at `/app/installations/{id}/access_tokens`. There is no refresh
token to hand over, and handing Google the App's private key is not on the table.

The credential that *would* fit `oauth2` natively is a GitHub App **user-to-server** token, which
does carry a refresh token and would be refreshed by Google for free. That is rejected: it acts as a
person rather than as the App, which gives up precisely the scoping that
[judge-gated-engineer-side-effects.md](judge-gated-engineer-side-effects.md) chose an installation
token for. Trading the blast radius for someone else's refresh loop is the wrong side of that trade.

So the App keeps minting, and we keep rotating. `bearer_token` is the type, and the `scheme` split
the previous design already needed — REST takes `Bearer <token>`, git-over-HTTPS takes
`Basic base64("x-access-token:<token>")` — survives it, but not for free. `prefix` prepends text to
the stored value; it cannot encode it. A single credential holding the raw token would therefore
render `Basic ghs_...` for the git rule, which is a 401.

**One mint, one stored credential per scheme.** A run's `github_app` credentials are minted once and
stored as separate ids: the bearer rule holds the raw token, the basic rule holds
`base64("x-access-token:<token>")` under `prefix: "Basic"`. The encoding stays ours, as it is today;
what moves is only where the encoded value lands. Rotation writes every scheme's id from one mint,
so the ids never hold *unrelated* tokens — but two `PATCH`es are two requests, and one can fail
while the other lands. The residual that leaves is bounded rather than designed away; see
"Deliberate simplifications".

### Rule of Two

The letters do not move. Injecting a GitHub credential still adds **[B]** to a profile that acts
**[C]** and reads the open web **[A]**, and every mitigation the previous design enumerated —
installation scope, `taint_sink_class: "sandbox_network"` under `enforce`, the allowlist's own
breadth — applies unchanged and remains the load-bearing part.

One thing genuinely changes, and it is a widening worth naming: **the token now persists outside a
run.** Under `transform` a minted token existed only inside interactions we had submitted; between
runs, nothing anywhere held a usable GitHub credential. Now one sits in Google's credential store
continuously, refreshed on a timer, whether or not anybody is using `coder`.

What bounds it:

- It is **write-only**. No endpoint returns a stored credential's value; create, update and list all
  answer with metadata only. Google holds it; we cannot read it back, and neither can anything that
  compromises our config.
- It is **still an installation token**, still ~1h, still scoped to the repositories the App is
  installed on with the permissions that installation was granted. Rotation replaces one short-lived
  token with another; it never mints a longer-lived one. The ceiling this design removes is on *run
  length*, not on *credential lifetime*.
- Its id is **deployment-scoped**, because the store is keyed by the API project. Two deployments
  sharing a project must not share an id, or one silently authenticates as the other.

What is not bounded away, and is accepted: between runs there is a live credential where previously
there was none. Anyone who can submit an interaction against our API project can reference it by id
and inherit the App's access without ever holding the token. That is the real cost of per-request
resolution, and it is the reason the API project's key is now as sensitive as the App's private key
was — a fact worth writing into the operator documentation rather than leaving implicit.

## Work plan

Each milestone stands alone and is verifiable without the next.

1. **Confirm mid-run propagation.** Everything below rests on a stored credential's value reaching
   an *already-running* interaction. This is asserted by the documented per-request resolution but
   has **not been observed**; see "Unverified assumption". Outcome: a recorded run showing the
   post-rotation value arriving at the sandbox's egress. If it does not propagate, the rest of this
   document is void and the ceiling stands — stop here.
2. **Credential store client.** Create, update and delete against the store, behind the same
   `AntigravityEgressError` contract the current resolver uses: a credential that cannot be
   established raises rather than resolving to a rule without one, because a sandbox that reaches a
   private repo unauthenticated fails as a 404 deep inside the agent. Verified by unit tests over a
   faked transport, and by the credential lifecycle against the live API.
3. **Route minted credentials to the store.** A rule naming a minted kind carries a `credential` id
   instead of a built header; a rule naming a static one is untouched, so `transform` stays for
   exactly that case. Verified by the existing shipped-profile and egress tests, re-pointed — a
   profile configuring no credential must still send no `network` block at all, which is what keeps
   the shipped `coder` at [C].
4. **Rotation task.** Periodic mint-and-`PATCH` at a fraction of token life. Verified by a test that
   drives it on a fake `Clock` — the existing egress tests already establish that pattern, so expiry
   is exercised without sleeping.
5. **Documentation.** `CONFIGURATION_REFERENCE.md` for the credential id and the API-project
   sensitivity above; a correction to the superseded paragraph in the previous design doc.

## Unverified assumption

**A mid-run `PATCH` reaches a running interaction.** Partially probed against the live API on
2026-09-18:

- `antigravity-preview-09-2026` accepts a submit carrying an allowlist rule with a `credential` id.
- `POST /credentials` creates a `bearer_token` credential; the response carries metadata only, never
  the token.
- `PATCH /credentials/{id}` replaces the token in place and advances `update_time`, touching no
  interaction and no configuration.
- `DELETE /credentials/{id}` removes it.

The step that could not be closed is the one that matters: the probe key creates interactions but
`GET /interactions/{id}` answers `not_found` for them, fresh or otherwise, so the header the sandbox
actually received after rotation was never read back. The experiment to finish, on a key that can
read interactions: start a background run that polls a header-echoing endpoint on a sleep loop
through an allowlist rule bound to a credential, `PATCH` the credential mid-run, and read the run's
own output for the value it saw before and after. Milestone 1 is that experiment, and nothing should
be built on the strength of the documentation alone.

## Deliberate simplifications

- **One credential per deployment, not per run or per user.** Unchanged from the previous design and
  for the same reason: the App installation is a property of the deployment. Per-run credentials
  would also defeat the point — a credential created at submit is frozen at submit again, just with
  more machinery around it.
- **Rotation is unconditional.** It does not ask whether a run is in flight. Gating it on live runs
  would add exactly the state the design is trying not to grow, to save a token exchange that costs
  one HTTP round trip.
- **Partial rotation is repaired by the next tick, not by a retry path.** Two ids mean two requests,
  and nothing makes them atomic. A tick that updates one and not the other leaves the schemes
  holding tokens of different ages — harmless while both are unexpired, since each is independently
  valid. The next unconditional tick rewrites both from a fresh mint, so drift self-heals within one
  interval, which is a fraction of token life. Sustained failure is not a new failure mode: it is
  the "rotation stopped" case above, arriving as a visible 401 on whichever scheme went stale first.
  Adding per-id retry, ordering or compensation would buy a narrower window at the cost of exactly
  the lifecycle state this design refuses to grow.
- **No cleanup of orphaned ids.** A credential whose config stopped referencing it keeps being
  rotated until an operator deletes it. Reconciling the store against config is machinery for a rare
  case, and it is unnecessary *because* of the rule above: every stored value is a minted token that
  expires on its own, so an orphan stops being a credential within the hour once rotation stops
  writing it. This bullet would not survive a static token in the store — an orphan would then stay
  live indefinitely, and config removal would silently fail to revoke it. That is the failure the
  store's scope is drawn to exclude, not one to add cleanup for.

## Non-goals

- **`oauth2` and `environment_variable` credential types.** Neither has a caller.
  `environment_variable` injects into the sandbox's process environment, which is the property this
  whole design exists to avoid.
- **Registered environments.** Still out, for the reason the previous design gave: reusing one
  across runs breaks the fresh-sandbox-per-run property the profile's isolation rests on.
- **Widening the App installation.** Out of scope here and a security change of the same weight as
  anything in this document, as the previous design already records.
