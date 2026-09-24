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

So the App keeps minting, and we keep rotating — for the credentials the store can actually carry,
which is fewer than it first appears.

**The store emits `Authorization: Bearer <token>` and nothing else.** A `bearer_token` credential
accepts `header_name` and `prefix` on create, but the egress proxy ignores both: a credential
created with `header_name: "X-Custom-Hdr"` and `prefix: "Basic"` arrives at the destination as
`authorization: "Bearer <token>"` (measured 2026-09-18; `prefix` is the control, having no alternate
spelling to blame). So the store cannot express GitHub's git-over-HTTPS form,
`Basic base64("x-access-token:<token>")`, and no arrangement of stored values fixes that — a
pre-encoded value would simply arrive as `Bearer <base64…>`, the wrong scheme.

**The scheme therefore decides the mechanism.** A credential goes to the store when both hold:

1. its value expires, so it must change while a run is in flight; and
2. the store can express its wire form — `Authorization: Bearer <token>`.

`github_app` on `scheme: "bearer"` satisfies both: that is the REST rule (`api.github.com`), and it
gains refresh. `scheme: "basic"` satisfies only the first, so the git rule (`github.com`) keeps the
`transform` header it uses today and keeps the ~1h ceiling with it. Static credentials fail the
first and stay on `transform` for the reasons above.

Both halves of that constraint are measured rather than assumed. **GitHub's git-over-HTTPS rejects
`Bearer`**: against a private repository with a real `ghs_` installation token, `upload-pack`
answered 401 unauthenticated, 200 under `Basic`, and 401 under `Bearer`, with
`WWW-Authenticate: Basic realm="GitHub"` on the refusal. `receive-pack` — the push case this ceiling
actually bites — separates the two layers: `Basic` reaches authorization and is refused 403 for
write, while `Bearer` never authenticates at all and stops at 401. So a push fails no differently
from a fetch, and no permission grant would change it.

The split is therefore structural, not a gap waiting to close. It could only change if GitHub began
accepting `Bearer` on git, which its own `WWW-Authenticate` advertises against; a design that
assumed otherwise would be betting on that.

### Pushing without git

A push is the step that matters most and comes last, so the git ceiling would defeat the point of
long runs on its own. The way around it is not to push over git at all. GitHub's Git Data API
(blobs, trees, commits, refs) lives on `api.github.com` and takes `Bearer`, which is the rule the
store keeps fresh. Anything `git push` does to a branch can be done through it.

So when a run's resolved network sends `api.github.com` with a stored credential, the sandbox gets a
small push helper mounted beside any attachments, and the agent's system instruction tells it to
push with that instead of `git push`. The agent still clones and commits with ordinary git. Cloning
happens at the start, well inside the first hour. The helper replays each commit the remote lacks
through the API, with the original author, committer, dates and message. Trees are
content-addressed, so the pushed commits keep their local ids. Where GitHub serialises one
differently, later commits are re-parented onto GitHub's copy and the helper says so. It sends no
credential itself: the proxy attaches the rotating one. It refuses a push that isn't a fast-forward
unless told `--force`, as git does.

The decision is made from the resolved network block, not from config, because the mount and the
instruction are only useful together and only when the REST credential actually outlives git's.

What still expires with the git token is git itself after the first hour: a late `git fetch` or
`git pull` fails. That is accepted. A long task pushes its own work, and it rarely needs to fetch
someone else's.

The one push the API cannot make is the first into an empty repository: GitHub refuses to write
objects or refs to a repository with no commits. The helper falls back to `git push` for that, which
works only while the submit-time token is valid. Creating a repository from scratch is the uncommon
case, and it pushes its first commit early anyway.

Git LFS content is the other thing the API cannot carry: it goes to GitHub's LFS server with git's
credential. The helper refuses a push that includes an LFS pointer rather than pushing the pointer
alone, and says to use `git push` while that still works.

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

1. ~~**Confirm mid-run propagation.**~~ **Done** — see "What was verified". A rotation mid-run
   reached the running interaction, so the rest of this plan stands. Milestones 2 to 5 have shipped.

2. **Credential store client.** Create, update and delete against the store, behind the same
   `AntigravityEgressError` contract the current resolver uses: a credential that cannot be
   established raises rather than resolving to a rule without one, because a sandbox that reaches a
   private repo unauthenticated fails as a 404 deep inside the agent. Verified by unit tests over a
   faked transport, and by the credential lifecycle against the live API.

3. **Route qualifying credentials to the store.** A rule whose credential expires *and* whose wire
   form is `Authorization: Bearer <token>` carries a `credential` id; every other rule keeps its
   built header, so `transform` stays for the git and static cases. Verified by the existing
   shipped-profile and egress tests, re-pointed — a profile configuring no credential must still
   send no `network` block at all, which is what keeps the shipped `coder` at [C].

4. **Rotation task.** Periodic mint-and-`PATCH` at a fraction of token life. Verified by a test that
   drives it on a fake `Clock` — the existing egress tests already establish that pattern, so expiry
   is exercised without sleeping.

5. **Documentation.** `CONFIGURATION_REFERENCE.md` for the credential id and the API-project
   sensitivity above; a correction to the superseded paragraph in the previous design doc.

6. **Push without git.** The helper and its mount, as above. Verified against a fake GitHub that
   implements the Git Data API with git's own plumbing over a bare repository, so a push counts as
   correct only when the branch lands on the local commit id. Still to verify live: one run against
   a real repository through the proxy, pushing after the git token has expired.

## What was verified

**A mid-run `PATCH` reaches a running interaction.** Measured against the live API on 2026-09-18.

A synchronous agent run polled a header-echoing endpoint five times, twenty seconds apart, through
an allowlist rule bound to a stored credential. The credential was `PATCH`ed from one sentinel value
to another 55 seconds in. The run's own transcript shows iterations 1-2 carrying the pre-rotation
value and iterations 3-5 carrying the post-rotation one. Per-request resolution is therefore
observed behaviour, not an inference from the documentation, and the ceiling this design exists to
remove is genuinely removable.

Also established in the same session:

- `POST`, `PATCH` and `DELETE /credentials/{id}` all behave as documented; no endpoint ever returns
  a stored value.
- `header_name` and `prefix` are accepted on create and then ignored on the wire, which is what the
  scheme rule above is built on.
- A submit naming `antigravity-preview-09-2026` accepts an allowlist rule carrying a `credential`.

One incidental finding, recorded because it shapes how this is testable: the probe key creates agent
interactions but `GET /interactions/{id}` answers `not_found` for them, while a plain model
interaction reads back normally. A **non-background** agent run sidesteps that entirely — its create
response carries the whole step transcript — which is how the experiment above was run and how any
future one should be.

## Deliberate simplifications

- **One Gemini API key per deployment.** The rotation task writes with the deployment's
  `gemini_api_key`, not with a key taken from the `coder` profile's own client. Every Google client
  reads that same key today, and a deployment with a different key per profile is a configuration
  this design does not support. If one ever appeared, rotation would write to the wrong project and
  the stored token would age out into a visible 401. It would fail loudly, never silently.

- **One credential per deployment, not per run or per user.** Unchanged from the previous design and
  for the same reason: the App installation is a property of the deployment. Per-run credentials
  would also defeat the point — a credential created at submit is frozen at submit again, just with
  more machinery around it.

- **Rotation is unconditional.** It does not ask whether a run is in flight. Gating it on live runs
  would add exactly the state the design is trying not to grow, to save a token exchange that costs
  one HTTP round trip.

- **Rotation writes one id, so there is no drift to reconcile.** The scheme rule leaves exactly one
  credential in the store — the bearer/REST one — so a tick is a single mint and a single `PATCH`.
  An earlier draft put one credential per scheme in the store and had to account for two `PATCH`es
  drifting apart; the capability constraint removed that case rather than requiring machinery for
  it. A failed tick is repaired by the next one, within a fraction of token life, and sustained
  failure is the "rotation stopped" case above arriving as a visible 401.

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
