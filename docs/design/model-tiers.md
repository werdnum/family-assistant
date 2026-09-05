# Model Tiers: Decoupling Model Choice from Processing Profiles

## Status

Proposed for approval before implementation.

## Goal

Make it possible to add and select stronger (or cheaper) models — e.g. `claude-fable-5` as a
first-class option, or a future `gpt-6-astra` — without creating a new service profile per model.

## Problem

A service profile currently bundles two independent axes into one object:

1. **How the agent operates** — tool policy, system prompt, context providers, `max_iterations`,
   Rule-of-Two classification, taint policy, visibility grants.
2. **How inference runs** — the model, via `processing_config.retry_config` or `llm_model`, with
   per-model request parameters in the top-level `llm_parameters` map.

`complex_tasks` is the clearest symptom: it is essentially "`default_assistant`'s tool surface, a
stronger model, more iterations". Adding an even stronger model today means either editing that
profile in place (losing the mid tier) or cloning it into an `even_more_complex_tasks` profile —
forking the entire tool-policy/prompt bundle to change one field. The profile matrix would grow
multiplicatively as models churn. `research` vs `research_max`
([gemini-deep-research-tiers.md](gemini-deep-research-tiers.md)) is the same pattern appearing a
second time, and the proposed classifier router
([classifier-routing-processing-profiles.md](classifier-routing-processing-profiles.md)) needs
hidden `default_assistant_fast/balanced/complex` clone profiles for the same reason.

## Decision summary

- Introduce a top-level `model_tiers` map: tier name → a **model recipe** (retry chain plus
  per-entry inference parameter overrides). Profiles reference a default tier instead of inlining
  models. Inline `retry_config`/`llm_model` remains valid for profiles that pin a model deliberately
  (see "Pinned profiles" below).
- Carry the selected tier in a validated **request/run envelope** — selected profile, resolved tier,
  authorization provenance — resolved and frozen once per run. Slash commands, the web UI, and
  `delegate_to_service` are all just ways to populate that envelope; they share one resolution,
  compatibility-validation, and authorization path. The registry's shared service configuration is
  never mutated.
- A tier controls **how inference runs** (models, fallback order, reasoning effort, thinking config,
  output limits). A profile controls **how the agent operates** (tools, prompts, context, iteration
  budget, taint/visibility). A stronger tier must never silently buy more actions.
- Tier admission is authorized as its own decision that a delegation allow cannot override, the
  **resolved** tier is what gets authorized (not just an explicitly supplied argument), and
  authorization covers **every model in the recipe**, not only the primary.
- Untrusted-input profiles can never cause expensive-tier execution, directly or through
  intermediaries. Assistant-chosen escalation to the top tier is confirm-gated; an authenticated
  user explicitly selecting a tier supplies that authorization directly, within operator policy.
- Tier names are capability-relative service classes (`standard`, `deep`, `frontier`), with exact
  model IDs visible in tier definitions, run records, and UI detail. Remapping a tier to a model
  that substantially changes cost, provider, or data handling is an explicit operator decision.

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
      - provider: "anthropic"
        model: "claude-fable-5"
  frontier:
    chain:
      - provider: "openai"
        model: "gpt-6-astra"
        llm_parameters:
          reasoning_effort: "xhigh"
      - provider: "anthropic"
        model: "claude-fable-5"

service_profiles:
  - id: "default_assistant"
    processing_config:
      model_tier: "standard"
    allowed_model_tiers: ["standard", "deep"]
  - id: "complex_tasks"
    processing_config:
      model_tier: "deep"
    allowed_model_tiers: ["deep", "frontier"]
```

Per-entry `llm_parameters` overlays the global model-keyed `llm_parameters` map, which stays the
source of model defaults. This lets one model run at different efforts in different tiers without
changing every use of that model. Effort values are provider-specific; a tier definition names the
value for the model it names, and no code assumes `"high"` means the same thing across providers.

`processing_config` gains `model_tier` as a third mutually exclusive way (with `retry_config` and
`llm_model`) of naming the profile's models. This requires widening the retry schema: the current
`RetryModelConfig` accepts only `provider`/`model` and forbids extras, and the retry client builder
feeds both entries the shared global `llm_parameters`; tier entries carry validated per-entry
overrides instead.

Adding a new model becomes: one `llm_parameters` entry (if it needs request parameters) plus one
tier-map edit. No profile changes.

## Per-request tier selection

`delegate_to_service` gains an optional `model_tier` argument. Slash-command variants (e.g.
`/complex frontier`) and a web-UI selector set the same envelope field. Resolution order: explicit
request value, else the target profile's `model_tier` default, else the profile's inline model
config. The resolved value is validated against the profile's `allowed_model_tiers`, checked for
compatibility (below), authorized, then frozen into the run.

Rejected alternative — per-request `processing_config` overlays: that object carries prompts,
context controls, and delegation restrictions alongside model settings; exposing a general overlay
would let request-time input cross exactly the profile/tier boundary this design establishes.

Rejected alternative — an escalation ladder inside `retry_config` ("retry on a stronger model when
the task seems hard"): `retry_config` is deliberately about transient errors, models self-assess
task difficulty poorly, and whole-run escalation would repeat completed side effects (calendar
writes, sent messages). If escalation is ever wanted, it is a new, separately designed mechanism
that continues from recorded progress; it is out of scope here.

## Authorization and abuse control

Tier admission is enforced as its own gate in the shared envelope-resolution path, for three reasons
the existing policy machinery alone cannot cover:

- The policy builder injects a synthetic self-delegation ALLOW at the `profile` layer, above a
  profile's own `tools_policy`. That rule assumes self-delegation cannot escalate privileges; a
  spend-selecting argument breaks that assumption, so a delegation allow must not be able to
  override tier admission.
- `ToolMatcher` argument rules match only arguments that are present. An omitted `model_tier`
  resolving to an expensive profile default would sail past `argument_equals` rules, so it is the
  **resolved** tier that is authorized.
- A cheap tier whose fallback entry is an expensive model would defeat gating that looks only at the
  primary, so admission covers the whole recipe.

Policy stance shipped by default:

- Profiles that process untrusted input (`email_intake`, `media_analyst`, event handlers) have no
  `allowed_model_tiers` beyond their default, and requests carrying untrusted taint cannot select an
  elevated tier through any intermediary. Tier authorization provenance rides the same origin/taint
  continuity that delegation already persists for queued runs.
- An assistant *choosing* to run at `frontier` requires user confirmation. The confirmation binds to
  the resolved recipe: `_durable_authorization_matches`-style comparison must include the resolved
  tier, so a tier remap or an omitted field cannot change what runs after approval.
- An authenticated user explicitly selecting a tier (slash command, UI selector) is itself the
  authorization, within `allowed_model_tiers`.
- Child delegations get their target's default tier. A parent running at `frontier` is not blanket
  authorization for expensive descendants; elevating a child requires its own request and admission.
- Queued/async runs persist the resolved recipe. A restart or configuration deployment must not
  silently change the models of an already-authorized run.

Spending *budgets* (per-day caps) are a loss-bounding control, not a permission mechanism, and are
out of scope for v1.

## Compatibility constraints

Model substitution is a constraint problem, not a ranking: a frontier model is not a substitute for
every standard model.

- **Pinned profiles.** Profiles whose behavior is provider-coupled keep inline model config and
  accept no tier override: `media_analyst` (Gemini is the only adapter representing audio/video; its
  lack of `retry_config` is deliberate), `browser_visual_profile` (computer-use validation already
  rejects non-Google providers and retry chains), `coder`, and the deep-research profiles (model
  *is* the product tier there). v1 allows tier overrides only on ordinary conversational profiles;
  extending further requires representing the capability requirement explicitly and validating the
  entire chain against it at startup.
- **Attachment/fallback behavior is preserved, not papered over.** The default profile documents
  that attachment injection is built by the primary's adapter and a fallback turn may receive an
  actionable note instead of the media. Tier recipes inherit that documented behavior; no claim is
  made that every entry in a chain has equivalent perception.
- **Prompts stop naming models.** The `complex_tasks` prompt currently says "powered by OpenAI
  GPT-5.6-sol", already wrong on its Fable fallback and nonsensical under tier selection. Behavioral
  prompts describe the role; the actual provider/model surfaces from runtime metadata (run records,
  UI detail, trace spans).

## What happens to existing profiles

- `complex_tasks` **stays**. It is a workflow — distinct prompt, 100-iteration budget — not a model
  alias. It defaults to `deep` and may be authorized for `frontier`. Whether it eventually dissolves
  into "`default_assistant` + tier" is a later decision that tier extraction does not force.
- `default_assistant` defaults to `standard` and may allow `deep` for users who want stronger
  inference without the `complex_tasks` workflow.
- The shipped tier-equivalent pairs (`research`/`research_max`) are left as-is in v1; deep-research
  models are pinned by design.
- The classifier-router design composes with this one: its hidden per-tier clone profiles collapse
  into one target profile plus a router-selected tier on the envelope, and its target-equivalence
  validation becomes structurally guaranteed for the tier axis (a tier *cannot* differ in tools,
  context, or policy). If the router ships first, its internal profiles are a migration source; if
  this ships first, the router routes among tiers. Router-selected tiers are assistant-chosen for
  authorization purposes (the router never selects a tier the profile's policy would not admit
  without confirmation).

## Observability

Run records, delegation results, and trace spans carry the resolved tier name and the exact model(s)
used, including whether a fallback entry served the run. The web UI shows the tier and model on the
turn/run detail. Logs never include the classified content, only the selection metadata.

## Deliberate simplifications

Accepted residual behavior, recorded so review does not re-litigate it:

- **No automatic difficulty routing in this design.** The delegating assistant (and the separately
  proposed classifier router) remains the router; this design only supplies the mechanism they
  select over.
- **No spend budgets in v1.** Hard eligibility plus confirmation bounds who can spend; aggregate
  caps come later if needed, and must then account for concurrent runs, retries, and descendants.
- **No named-model presets.** Capability tiers cannot express "I specifically want Fable". If that
  becomes a product requirement it is a separately authorized preset, not a profile.
- **Cross-model `resume_delegation_id` keeps current semantics.** Resuming a delegation under a
  different tier replays history across providers exactly as a fallback turn does today,
  provider-specific reasoning metadata included. Known limitation, documented rather than solved.
- **Tiers do not carry `max_iterations`.** Iteration budget is agent behavior and stays on the
  profile, even though this means "deep thinking" and "long-running" remain separately configured.

## Work plan

Each milestone lands independently; construction detail (field names, plumbing) is decided in the
implementing PRs and verified by the type checker, tests, and conformance rules.

1. **Tier substrate.** `model_tiers` config with per-entry parameter overrides; profiles may
   reference a tier; shipped defaults move `default_assistant` and `complex_tasks` onto
   `standard`/`deep` with unchanged effective models. Verified by config-model tests and the
   shipped-reasoning-config tests proving the resolved clients and parameters are identical to
   today's.
2. **Request/run envelope.** Tier resolution/freezing threaded through the shared execution path
   (sync and queued delegation, slash commands, web), with the resolved recipe persisted on queued
   runs. Verified by functional delegation tests covering resolution order and restart-stability of
   queued runs.
3. **Admission and authorization.** `allowed_model_tiers`, the tier-admission gate independent of
   delegation allows, resolved-default and full-chain coverage, confirmation binding including the
   tier, taint/origin continuity, child-default rule. Verified by policy unit tests including the
   self-delegation-allow and absent-argument cases, and a confirmation-drift test.
4. **Selection surfaces.** `delegate_to_service` argument, slash-command variant, web-UI selector;
   prompts scrubbed of model names; observability fields. Verified by web/telegram functional tests
   and a conformance check that shipped prompts name no model IDs.
5. **Documentation.** Operator reference for `model_tiers` and `allowed_model_tiers` in
   `docs/operations/CONFIGURATION_REFERENCE.md`; user-facing tier selection in the relevant
   `docs/user/` topic file; architecture docs updated to describe the profile/tier split.
