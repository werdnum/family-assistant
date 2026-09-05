# Model Tiers and Value-for-Money Model Routing

## Status

Proposed for approval before implementation. Revised after design review: the original tier
separation stands; this revision adds the product semantics — Auto routing, manual/auto eligibility,
override UX — and repositions the classifier router to select tiers rather than hidden profile
clones.

## Goal

Use inexpensive models where they are good enough, and selectively spend more on inference when
stronger reasoning materially improves the result. Make stronger models (`claude-fable-5` as a
first-class option, a future `gpt-6-astra`) available without creating a new service profile per
model.

This is a value-optimization problem, not a budgeting problem. The intended mental model fits in
three sentences:

- Assistant, Engineer, Browser, Research describe **what kind of agent** you are using.
- Auto, Standard, Deep and Max describe **how much model capability** to apply to this request.
- Leave it on Auto most of the time; pick Deep or Max when you know a request deserves more.

If ordinary use requires understanding more than that, the design is leaking configuration into the
product.

## Problem

A service profile currently bundles two independent axes:

1. **How the agent operates** — system prompt, tools, tool policy, context providers, taint
   behavior, visibility grants, `max_iterations`, specialist runtime behavior.
2. **How inference runs** — provider/model via `retry_config` or `llm_model`, with per-model request
   parameters in the top-level `llm_parameters` map.

Model choice churns much faster than agent roles, and the coupling forces bad outcomes: replace a
model in place (losing the previous cost/capability point) or clone the profile
(`even_more_complex_tasks`). `complex_tasks` is the clearest symptom — partly a long-task workflow,
but currently *defined* as the place to obtain a stronger model. `research` vs `research_max`
([gemini-deep-research-tiers.md](gemini-deep-research-tiers.md)) repeats the pattern, and the
classifier-router proposal
([classifier-routing-processing-profiles.md](classifier-routing-processing-profiles.md)) needs
hidden `default_assistant_fast/balanced/complex` clones purely because model choice lives inside
profiles.

Other agent harnesses have converged on the same separation: an agent identity represents durable
behavioral and capability differences; the model is independently variable where the runtime permits
(Antigravity's independent agent/model/effort controls, OpenCode's per-session model over configured
agents, Claude Code's model aliases and `opusplan` split). Processing profiles already provide the
durable agent abstraction; no second abstraction is needed.

## Core concepts

Three internal objects, two prominent user-facing controls:

- **Processing profile** — how the agent operates. If changing something changes what the agent can
  do or how it behaves operationally (including `max_iterations`), it belongs here.

- **Model tier** — a named model recipe: primary provider/model, availability-fallback chain, and
  per-entry inference parameters (reasoning effort, thinking config, output settings). Changing the
  tier leaves the profile unchanged.

- **Model selection policy** — a run requests either a specific tier or **Auto**. Auto is a routing
  policy over tiers, not itself a tier. It resolves to a concrete tier before execution starts, and
  the resolved tier is frozen for the run. The run envelope records both the requested selection
  (provenance) and the resolved tier:

  ```text
  requested selection: auto
  resolved tier:       deep
  actual primary:      openai / gpt-5.6-sol
  actual fallback:     anthropic / claude-fable-5
  ```

## Decision summary

- Introduce a top-level `model_tiers` map (tier → recipe). Profiles reference a default tier; inline
  `retry_config`/`llm_model` remains valid for deliberately pinned profiles.
- Carry the selection in a validated **request/run envelope** — profile, requested selection,
  resolved tier, provenance — resolved and frozen once per user turn, shared by every entry surface
  (slash commands, web UI, `delegate_to_service`). Shared service configuration is never mutated.
- A tier controls how inference runs; a profile controls how the agent operates. Changing
  intelligence must never silently change tools, authority, context visibility, iteration limit, or
  security properties — in either direction.
- **Auto is the normal state** on ordinary interactive profiles: choose the least expensive eligible
  tier likely to complete the request reliably; upgrade where additional reasoning capability is
  likely to materially improve correctness, judgment, or completion. Not "long request → Deep", not
  "uses tools → Deep".
- Two eligibility lists per profile: `allowed_model_tiers` (what may be explicitly chosen) and
  `auto_model_tiers` (what Auto may choose without further interaction). Initially Auto chooses
  between `standard` and `deep`; `frontier` requires explicit selection. Widening Auto later is a
  config change, not new architecture.
- **Auto routing within `auto_model_tiers` is not confirmation-gated.** Choosing Auto authorizes the
  configured Auto range; a router that asks "may I use Deep?" per turn destroys the value of
  routing.
- Manual overrides are **one-shot by default** (apply to the next request, then return to Auto),
  with an explicit, visibly persistent per-conversation pin available.
- The classifier router **selects tiers on one profile, not hidden clone profiles**. Conversation
  identity stays the profile identity; tier is run metadata, never a history namespace.
- Child delegations resolve profile and model selection independently at each agent boundary; a
  parent's expensive tier does not propagate.
- Tier names are capability-relative service classes. UI vocabulary (`Standard`/`Deep`/`Max`) and
  config vocabulary (`standard`/`deep`/`frontier`) need not be identical; exact model IDs stay
  visible in details, run records, and diagnostics. Remapping a tier to a model that substantially
  changes cost, provider, or data handling is an explicit operator decision.

## Configuration shape

Illustrative, not final construction detail:

```yaml
model_tiers:
  standard:
    chain:
      - provider: "google"
        model: "gemini-3.8-flash"
      - provider: "openai"
        model: "gpt-5.6-terra"
  deep:
    chain:
      - provider: "openai"
        model: "gpt-5.6-sol"
        llm_parameters:
          reasoning_effort: "high"
      - provider: "anthropic"
        model: "claude-fable-5"
  frontier:
    chain:
      - provider: "anthropic"
        model: "claude-fable-5"
        llm_parameters:
          output_config:
            effort: "xhigh"

model_routing:
  classifier:
    provider: "google"
    model: "gemini-3.8-flash"
    timeout_seconds: 10

service_profiles:
  - id: "default_assistant"
    processing_config:
      model_selection: "auto"
      model_tier: "standard" # fallback when routing is unavailable or disabled
    allowed_model_tiers: ["standard", "deep", "frontier"]
    auto_model_tiers: ["standard", "deep"]
    auto_routing_guidance: |
      Prefer standard for routine conversation, retrieval and straightforward
      tool use. Choose deep where substantial ambiguity, multi-stage reasoning,
      difficult judgement or conflicting evidence makes stronger reasoning
      materially more likely to improve the result.
  - id: "reminder"
    processing_config:
      model_tier: "standard"
    allowed_model_tiers: ["standard"]
```

Per-entry `llm_parameters` overlays the global model-keyed `llm_parameters` map, which stays the
source of model defaults. The same model at different efforts is a legitimate distinct
economic/capability point; effort and thinking semantics are provider-specific, so tiers deal in
validated recipes, never a universal numeric intelligence level. This requires widening the retry
schema (`RetryModelConfig` currently accepts only `provider`/`model` and forbids extras) and letting
tier entries carry validated overrides where the retry client builder today feeds both entries the
shared global map.

Adding a new model becomes one `llm_parameters` entry (if needed) plus a tier-map edit. No profile
changes.

## Auto routing

A separate cheap classification request before the main turn, so the weaker execution model is not
solely responsible for judging its own adequacy. The classifier receives bounded recent history, the
current trusted request, attachment metadata, the target profile identity, and descriptions of the
eligible tiers (including the profile's `auto_routing_guidance` — routing thresholds are contextual
to the profile: Engineer reaches for `deep` more readily than the Assistant). It returns a
constrained structured result containing only the tier.

The existing classifier-router proposal's mechanics — bounded history, structured output with an
enum of configured targets, timeout, explicit availability fallback, once-per-turn selection — carry
over with profile targets replaced by tiers. What disappears is that design's hardest part:
separating router conversation identity from hidden execution-profile identity. There is one
Assistant profile; the router never creates a new agent identity, and existing history needs no
migration. If classification fails or times out, the profile's configured `model_tier` is the
availability fallback. An explicit user selection bypasses the router entirely.

Routing runs once per user turn, before the tool loop, and the result holds for the whole run —
providers carry different reasoning metadata, tool representations, and attachment handling, and
attachment injection is built by the primary adapter. Deliberate hybrid patterns ("strong model
plans, cheap model executes", "Think harder" re-examination of a previous conclusion) are later,
separate work; "Think harder" in particular must reconsider a conclusion, not blindly re-execute a
turn that already produced side effects.

## Authorization

Tier admission is enforced as its own gate in the shared envelope-resolution path, because the
existing policy machinery alone cannot cover it:

- The policy builder injects a synthetic self-delegation ALLOW at the `profile` layer, above a
  profile's own `tools_policy`. That rule assumes self-delegation cannot escalate; a spend-selecting
  argument breaks the assumption, so a delegation allow must not override tier admission.
- `ToolMatcher` argument rules match only arguments that are present, so it is the **resolved** tier
  that is authorized, not just an explicitly supplied one.
- Admission covers **every model in the recipe**, not only the primary — a cheap tier with an
  expensive fallback would otherwise defeat the gate.

The policy itself is expressed through the two eligibility lists, keyed to who initiated and
authorized the run:

- An authenticated user's explicit selection is itself the authorization, within
  `allowed_model_tiers`.
- Auto — and any model-composed tier request, such as a `delegate_to_service` `model_tier` argument
  — is bounded by the target profile's `auto_model_tiers`, with no per-turn confirmation inside that
  range.
- **Untrusted evidence cannot create model-upgrade authority, but it does not erase model selection
  authorized by a trusted initiating request.** A forged email cannot instruct its way to
  `frontier`; an authenticated user saying "use Max to analyse this suspicious email" stays on Max
  even as the model reads attacker-controlled content, and an Auto turn routed to `deep` is not
  demoted because it then browses the web. Provenance rides the same origin/taint continuity
  delegation already persists — no separate notion of "expensive taint".
- Unattended untrusted-input profiles (`email_intake`, event handlers) configure a fixed tier or a
  deliberately narrow Auto range.
- Queued/async runs persist the resolved recipe; a restart or configuration deployment must not
  silently change the models of an already-created run.

Availability fallback and capability routing stay distinct mechanisms: a retry chain answers "what
if the selected model is unavailable", never "the cheap model's answer was bad, replay everything on
the expensive one" — which is unsafe after side effects.

## User experience

The composer keeps the profile selector and gains a small intelligence control beside it:

```text
[ Assistant ▾ ]                    [ Auto ✦ ▾ ]
```

The intelligence menu offers **Auto** (best value per request), **Standard**, **Deep**, and **Max**
(strongest configured recipe), with exact models as secondary detail. For most users the stable
visible state is `[Assistant] [Auto ✦]` and the second control is never touched. Selecting Deep or
Max applies to the next request and returns to Auto; a "use Deep for this conversation" pin is
available and visibly persistent. Profiles that expose fewer tiers simply omit the inapplicable
choices; a reminder profile may expose none.

Routing is visible without dominating the UI: turn/run detail shows the equivalent of
`Auto → Deep · GPT-5.6 Sol` (requested selection, resolved tier, actual model, fallback use). The
profile selector stops foregrounding model IDs and tool inventories once intelligence is
independently selectable. The same semantics eventually appear across web, iOS, and Telegram without
inventing one command per profile/model combination.

**Telegram** has no composer control, but its profile slash commands are already per-message
(`/complex <text>` routes that one message), which is exactly the one-shot semantics chosen above.
Tier selection therefore surfaces as per-message tier commands: `/deep <request>` and
`/max <request>` run that message on the conversation's current profile at the named tier, and a
plain message stays on Auto. Tier commands and profile commands are separate commands, not a
combinatorial set; a message carries one or the other, and the rare "specific profile at a specific
tier" ask is served by the web UI (or, if demand appears, by letting a profile command accept a
leading tier word — reasonable-behaviour territory, decided in the implementing PR). A persistent
per-conversation pin on Telegram is deferred; one-shot commands cover the value.

## Compatibility constraints

Model substitution is a constraint problem, not a ranking. Profiles whose runtime is provider- or
capability-coupled stay pinned initially: `media_analyst` (only the Gemini adapter represents
audio/video; its missing `retry_config` is deliberate), `browser_visual_profile` (computer-use
validation already rejects non-Google providers and retry chains), `coder`, and the deep-research
profiles (the model *is* the product tier there). Extending tier selection to such a profile
requires representing the capability requirement explicitly and validating the entire chain against
it at startup; incompatibility is explicit and validated, never discovered mid-request.
Attachment/fallback behavior keeps its documented semantics — no claim that every chain entry has
equivalent perception.

Behavioral prompts stop naming models (the `complex_tasks` prompt's "powered by OpenAI GPT-5.6-sol"
is already wrong on its Fable fallback and nonsensical under tier selection); the actual model
surfaces from runtime metadata.

## Existing profiles

- `complex_tasks` remains initially for compatibility, but **stops being the canonical way to obtain
  a stronger model** — that becomes `Assistant + Deep/Max`. Its long-term survival depends on a
  simple test: if `default_assistant` and `complex_tasks` used exactly the same model, would both
  still need to exist? If its long-autonomous-investigation workflow (distinct guidance,
  100-iteration ceiling) proves genuinely distinct, it survives as a workflow; if not, `/complex`
  collapses to a compatibility alias approximating "Assistant with the complex workflow and Deep
  intelligence". That decision is deferred, not forced by tier extraction.
- `engineer` is the clearest beneficiary: its identity is diagnostic instructions, read-only access,
  specialist policy and sandboxed reproduction — the model is replaceable, so
  `Engineer + Standard/Deep/Max` works without `engineer_opus`/`engineer_fable` clones, with its own
  more-eager routing guidance.
- The classifier-router design is simplified rather than discarded: its classifier mechanics
  survive; its hidden clone profiles and history-identity separation become unnecessary. That doc
  should be revised to target tiers when routing is implemented.

## Runtime architecture

The run needs a resolved inference binding, not mutation of shared configuration.
`ProcessingService` owns an `llm_client` and builds model-dependent helpers (attachment processing,
the streaming loop) from it, so selection resolves **before** model-dependent turn preparation:

```text
profile config + requested selection (+ Auto routing)
  → resolve tier → validate compatibility → build run-scoped inference binding
  → prepare attachments/context → run the full LLM/tool loop
```

Conversation identity remains the profile identity: a conversation with the Assistant is an
Assistant conversation whether successive turns resolve to Standard, Deep, or Max. Tier is run
metadata inside that identity, never another history partition — which is also why tier routing is
much simpler than hidden-profile routing.

## Observability and evaluation

Run records, delegation results, and trace spans carry requested selection, resolved tier, actual
serving model, effort, and fallback use. Telemetry should answer value questions, not just spend:
tier distribution of Auto decisions, classifier latency, manual-override and post-Auto-upgrade
rates, cost distribution by resolved tier and profile, fallback rates, and whether `deep` measurably
improves outcomes for the categories routed there. The guiding metric is successful useful work per
dollar at acceptable latency — measured through proxies and evaluation sets, using the existing
per-profile token-attribution work rather than a separate billing subsystem.

Auto is evaluated before it is trusted: the classifier first runs in **shadow mode**, recording
`would_choose` while the configured model executes, compared against human judgment and outcomes.
Auto then enables for the Assistant and Engineer first; pinned profiles stay outside the experiment.

## Non-goals

- **Task-level spend caps, reservation ledgers, or billing policy.** Value for money, not
  containment; aggregate cost is observability.
- **Tiers as capability grants.** A stronger model never gets more tools by being stronger.
- **Mid-run model switching.** One recipe per turn is the conservative baseline; hybrid patterns are
  later, explicit work.
- **A raw model picker in ordinary UX.** Provider churn is an operator concern; tiers keep the user
  concept stable while operators remap underneath. Exact-model presets ("specifically Fable") only
  if real need emerges, and never as profiles.
- **Difficulty escalation via retry.** Rejected above.

## Deliberate simplifications

Accepted residual behavior, recorded so review does not re-litigate it:

- Cross-model `resume_delegation_id` keeps current semantics: resuming under a different tier
  replays history across providers exactly as a fallback turn does today. Known limitation.
- Tiers do not carry `max_iterations`; "deep thinking" and "long-running" remain separately
  configured, on the profile.
- `research`/`research_max` stay as-is; deep-research models are pinned by design.
- Auto's initial range excludes `frontier` even where manual selection allows it: classifier false
  positives have asymmetric cost, and widening later is a one-line config change.

## Work plan

Each stage lands independently; construction detail (field names, plumbing) belongs to the
implementing PRs, verified by the type checker, tests, and conformance rules.

1. **Tier substrate, behavior-preserving.** `model_tiers` with per-entry parameter overrides;
   `default_assistant` and `complex_tasks` resolve through tiers with provably unchanged effective
   clients and parameters. Verified by config-model tests and the shipped-reasoning-config tests.
2. **Explicit per-request selection and observability.** The run envelope threaded through sync and
   queued paths with the resolved recipe persisted; eligibility lists and the tier-admission gate
   (self-delegation-allow, absent-argument, and full-chain cases covered by policy tests, plus a
   confirmation-drift test); `delegate_to_service` argument, slash-command variant, web selector
   with one-shot/pinned semantics; prompts scrubbed of model names (conformance-checked). At this
   point `Assistant + Deep` works without `complex_tasks`.
3. **Tier routing in shadow mode.** Classifier mechanics retargeted from hidden clone profiles to
   tiers; `would_choose` recorded while configured models execute; evaluation against outcomes.
4. **Auto by default** on Assistant and Engineer once shadow evaluation is acceptable; UI defaults
   to Auto; the classifier-routing design doc revised to match.
5. **Reconsider `complex_tasks`** against the same-model test; collapse to a compatibility alias or
   keep as a genuinely distinct workflow. Documentation throughout: operator reference for
   `model_tiers`/`model_routing`/eligibility lists in CONFIGURATION_REFERENCE.md, user-facing
   intelligence selection in `docs/user/`, architecture docs updated to the profile/tier split.
