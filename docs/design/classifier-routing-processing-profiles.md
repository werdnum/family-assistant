# Classifier-Routed Processing Profiles

## Status

Proposed for approval before implementation.

## Goal

Add a processing-profile kind that classifies each incoming turn and executes it with one of a
configured set of local processing profiles. The initial use is to make `default_assistant` choose
between fast, balanced, and complex model tiers without requiring the user or the current model to
delegate explicitly.

## Decision summary

- Route once per turn, before the selected profile prepares or executes the turn.
- Treat the router as the conversation's stable profile identity. All routed turns read and write
  history under `default_assistant`, even when different execution profiles are selected.
- Let the selected target profile own the system prompt, model, tool policy, context providers,
  maximum iterations, and taint policy for that turn.
- Require all targets of one router to have equivalent effective permissions. Classification is a
  cost/capability decision, not a security-boundary decision. Equivalence includes automatically
  injected context, not only tool and taint policy.
- Restrict v1 targets to local, non-router profiles. This prevents routing cycles and avoids mixing
  remote A2A lifecycle semantics into the first version.
- Use native structured output for the classifier decision. An invalid decision or exhausted
  classifier retry chain is an error unless the operator explicitly configures a fallback route.
- Keep routing configuration declarative and validate references and cycles at startup.

## Why the decision is per turn

Reclassifying after each tool result could move an in-progress task between models with different
reasoning state and provider-specific metadata. It would also add a classifier request to every LLM
iteration. Selecting once per `handle_chat_interaction` call gives the chosen profile the entire
tool loop and still allows the next user turn or system wake-up to choose a different tier.

## Configuration

`ServiceProfile` gains an optional `classifier_router` block. A profile must be exactly one of a
local concrete profile, a remote A2A profile, or a classifier router.

```yaml
indexing_llm_profile_id: "default_assistant_balanced"

service_profiles:
  - id: "default_assistant"
    description: "Automatically selects the appropriate general-assistant tier."
    classifier_router:
      classifier:
        provider: "google"
        model: "gemini-3.6-flash"
        retry_config:
          primary:
            provider: "google"
            model: "gemini-3.6-flash"
          fallback:
            provider: "openai"
            model: "gpt-5.5"
      prompt: |
        Select the least expensive profile that can reliably complete the current turn.
        Consider required reasoning depth, ambiguity, number of dependent steps, and the
        consequences of mistakes. Return only the structured route decision.
      history_messages: 6
      timeout_seconds: 10
      fallback_profile_id: "default_assistant_balanced"
      routes:
        - profile_id: "default_assistant_fast"
          description: "Simple conversation and straightforward single-step tasks."
        - profile_id: "default_assistant_balanced"
          description: "Moderate reasoning, ambiguity, or several dependent steps."
        - profile_id: "default_assistant_complex"
          description: "Deep, high-consequence, or unusually complex reasoning."

  - id: "default_assistant_fast"
    internal: true
    processing_config:
      provider: "google"
      llm_model: "gemini-3.6-flash"

  - id: "default_assistant_balanced"
    internal: true
    processing_config:
      provider: "anthropic"
      llm_model: "claude-sonnet-4-6"

  - id: "default_assistant_complex"
    internal: true
    processing_config:
      provider: "openai"
      llm_model: "gpt-5.5"
      max_iterations: 100
```

The exact model IDs remain ordinary deployment configuration. The shipped defaults should use models
already supported by the repository rather than couple the router implementation to a particular
generation name. Operators can substitute a future `gpt-5.6-sol`-style model without a code change.

`internal: true` hides execution-only profiles from slash commands, the web profile picker, the
delegation catalog, and inbound A2A profile selection. They remain addressable only as validated
router targets. Internal profiles cannot declare slash commands or be selected as the application
default. This avoids exposing three user-facing versions of the default assistant.

The classifier receives:

- the configured classifier prompt;
- the route IDs and descriptions;
- a bounded plain-text transcript of recent history stored under the router profile;
- the current trigger's text and attachment metadata (not attachment contents).

Its structured response contains only `profile_id`. The response schema uses a dynamically built
`Literal`/JSON Schema enum containing the configured route IDs, followed by an application-level
allowlist check. Free-form classifier reasoning is neither requested nor logged, preventing it from
echoing user content into logs.

`timeout_seconds` bounds the additional time before the selected target can emit its first token.
When `fallback_profile_id` is configured, exhausting the classifier's retry chain selects that
profile and emits an error-level operational event. Without it, the turn fails visibly. This is an
explicit availability policy, not an inferred route.

Profile-kind exclusivity is based on the operator's YAML shape: a profile may declare exactly one of
`remote_a2a` or `classifier_router`; absence of both means a concrete local profile. The config
loader does not merge `DefaultProfileSettings.processing_config`, tool configuration, prompts, or
policy into router profiles. It instead resolves the small router-facing service metadata needed for
timezone and delegation behavior separately. This avoids the default-factored `processing_config`
making every router appear to be a concrete profile.

## Runtime structure

Add `ClassifierRoutingService`, implementing `DelegatableService` with `kind = "local"`. It owns:

- router service metadata;
- a classifier `LLMInterface`;
- validated route metadata;
- references to target `ProcessingService` instances.

On a turn it:

1. Loads bounded recent history using the router profile ID.
2. Calls `generate_structured` for a route decision.
3. Validates the returned target against the configured route set.
4. Calls the selected target service with an internal `history_profile_id` override set to the
   router ID.
5. Returns the selected service's `ChatInteractionResult` unchanged.

It also implements `handle_chat_interaction_stream`. Streaming classification happens before the
target stream is opened; the router then proxies every target `LLMStreamEvent` unchanged and passes
through `reuse_existing_user_row`. No user-visible routing event is added in v1. This supports both
the web turn producer and inbound A2A streaming paths.

`ProcessingService` gains an internal-only history identity parameter. That identity is used for
recent-history reads and message-history writes. Tool execution, confirmations, visibility grants,
and audit/taint records continue to use the selected target's actual profile ID. Outbound delegation
authorization uses the public router ID, because the internal execution profile acts on behalf of
that router and existing target allowlists name `default_assistant`. This separation is intentional:

| Concern                                              | Identity                     |
| ---------------------------------------------------- | ---------------------------- |
| Conversation continuity                              | Router (`default_assistant`) |
| Outbound delegation source allowlist                 | Router (`default_assistant`) |
| Prompt, model, tools, confirmation, taint, and audit | Selected target profile      |

Existing `default_assistant` history remains readable without migration because the router retains
that ID. Only new execution-tier IDs are introduced.

### Asynchronous continuations

Conversation identity and execution identity must remain separate beyond the initial synchronous
call. Add `history_profile_id` (also described as the conversation profile in runtime context) next
to `processing_profile_id` wherever a routed turn can create durable follow-up work.

- A confirmation resume is part of the same turn: it resumes with the original selected execution
  profile and writes history using the stored router history ID.
- Scheduled callbacks, `wake_llm` automations, and error-notification wakes start new turns: records
  created by a routed turn retain the router as their conversation/source profile so the new turn
  re-enters the router and is classified again.
- Delegation runs store both the selected source execution profile and router history profile. A
  completion wake re-enters the router, while confirmation and audit checks retain the original
  execution profile.

The field is threaded through `ToolExecutionContext`, confirmation/task payloads, automation
provenance, and delegation-run persistence. Non-routed turns set both identities to the same profile
ID, keeping current behavior. Database changes require an Alembic migration and typed repository
updates; no fallback inference from an internal profile ID is permitted.

`ToolExecutionContext` also carries `delegation_source_profile_id`. A router sets it to its public
ID while retaining the target as `processing_profile_id`. `delegate_to_service` checks the target's
`allowed_delegation_sources` against `delegation_source_profile_id`; confirmation, taint, and audit
records still identify the concrete execution profile. Non-routed turns again set both values to the
same ID. This preserves existing allowlists such as
`allowed_delegation_sources: ["default_assistant"]` without expanding them to implementation-only
tier IDs.

The assistant setup becomes two-pass: instantiate concrete local and remote profiles first, then
instantiate routers after all target references can be resolved. Introduce a local chat-service
protocol covering `service_config`, non-streaming handling, and streaming handling instead of
requiring `isinstance(..., ProcessingService)` at web, A2A, and default-profile boundaries. The
configured default may be a concrete local service or router, but not a remote A2A service.

Current bootstrap code also reaches through the default service for `llm_client`, `tools_provider`,
and timezone. A router exposes its own router `service_config` (including timezone). Add the
top-level `indexing_llm_profile_id` setting for `DocumentIndexer`; it must reference a concrete
local profile and may reference an internal route target. When the default service is concrete, the
field defaults to that profile for backward-compatible behavior. When the default is a router,
startup requires an explicit value. Indexing never derives its client from `fallback_profile_id` or
route order, so chat availability policy and route reordering cannot silently change extraction
behavior. The existing `app.state.llm_client` compatibility value uses this same explicit utility
client (or is removed if an implementation audit confirms there are no consumers). Root UI/API tool
listings keep using the existing root provider; they do not use a target profile's policy provider.
Shutdown closes concrete profile providers once and treats routers as non-owning facades.

## Security constraints

- Classifier routes are an operator-controlled allowlist; model output cannot name arbitrary
  profiles.
- Router targets cannot be remote, internal routers, or the router itself in v1.
- Startup validation rejects missing targets, duplicate IDs, empty route sets, and router cycles.
- The classifier has no tools. It only chooses among configured IDs.
- Targets of a router must have identical resolved tool-policy, operator-policy, taint-policy,
  visibility grants, note-visibility constraints, delegation permissions, and wake permissions.
  Startup fails with a field-specific mismatch instead of treating classifier routing as a way to
  cross a security boundary.
- Targets must also have identical resolved context-provider descriptors. Bootstrap constructs a
  canonical descriptor before instantiating each provider and compares provider type plus every
  configuration input that controls which private data is injected. This includes effective notes
  visibility, calendar source/configuration, known-user maps, weather configuration, Home Assistant
  URL/template/SSL settings, context-related prompt templates, and included system documents. Secret
  values are compared by a non-reversible fingerprint and never included in mismatch errors. Adding
  a new context provider requires adding its complete construction inputs to the descriptor; an
  unregistered provider makes router validation fail closed.
- Runtime taint sources are passed unchanged to the selected target.

The shipped default tiers therefore differ only in model, model parameters, prompt guidance, and
iteration budget. A specialized profile with broader or narrower capabilities remains explicitly
selectable/delegatable and cannot be placed behind the same router.

Inbound A2A resolution rejects `internal` target IDs even if supplied directly in message metadata,
but accepts a non-internal router through the local chat-service protocol. The same visibility rule
applies to all profile lookup surfaces rather than only presentation catalogs.

## Observability

Create a `processing.profile.route` trace span with router ID, selected target ID, classifier model,
latency, and whether the explicit fallback was used. Log the same fields without the classified
transcript or model-generated free text. Errors name the router and classifier model but do not log
user content.

The diagnostics profile inventory should identify routers and list target IDs, while continuing to
redact prompts and policy bodies from read-only diagnostics.

## Tests

01. Config-model tests for valid router config, mutually exclusive profile kinds, duplicate IDs,
    missing/internal constraints, and cycles.
02. Unit tests with fake classifier and target services proving simple/balanced/complex selection,
    invalid-output failure, configured fallback behavior, timeout behavior, one classification per
    turn, and independent concurrent turns.
03. Functional message-history test: two consecutive turns select different targets, the second
    target receives the first turn, and all user-visible rows remain under the router history ID.
04. Functional policy test: the selected target's profile ID reaches `ToolExecutionContext`, the
    router ID is used for outbound delegation allowlists, and history remains under the router ID.
05. Streaming functional tests proving web and inbound A2A calls classify once, proxy target events,
    and preserve `reuse_existing_user_row` behavior.
06. Confirmation-flow test: approve a confirmation created during a routed turn, then assert the
    resumed execution uses the target policy and writes its result under the router history ID.
07. Callback, automation wake, and delegation-completion tests proving new turns re-enter the
    router.
08. Web/A2A/profile-catalog tests proving internal route targets are neither listed nor directly
    addressable and the router remains selectable as `default_assistant`.
09. Config/bootstrap tests proving differences in every context-provider input reject a route set,
    secrets stay out of validation errors, and indexing requires an explicit concrete profile when
    the default is a router.
10. Indexing test proving route reordering and classifier fallback changes do not change the
    `DocumentIndexer` client.
11. Existing processing-profile, Telegram, web chat, delegation, diagnostics, and task-worker tests.

## Documentation changes

- Add an operator-facing section to `docs/user/USER_GUIDE.md` describing automatic per-turn model
  selection and how explicit slash-command profiles bypass the default router.
- Put the default classifier prompt in `prompts.yaml` (or the resolved router prompt configuration)
  so classification behavior is explicit and editable.
- Update architecture/profile documentation to distinguish conversation identity from execution
  profile identity.

## Implementation milestones

1. Add typed router configuration, presence-aware startup validation, permission/context-provider
   equivalence checks, explicit indexing-client configuration, and config-loader merge support.
2. Add durable history-identity separation across processing, tools, confirmations, automations,
   delegation runs, and task payloads, covered by focused functional tests and a migration.
3. Implement structured classification plus streaming/non-streaming `ClassifierRoutingService` paths
   with timeout, explicit fallback, and trace/log metadata.
4. Update default-service consumers, local service protocols, delegation source identity, shutdown
   ownership, and inbound A2A visibility enforcement.
5. Convert shipped `default_assistant` into a router and add hidden fast/balanced/complex targets.
6. Update profile catalogs, diagnostics, user documentation, and classifier prompt.
7. Run formatting/linting, focused tests, plausibly impacted suites, then `poe test` because this is
   a major processing-path change.
