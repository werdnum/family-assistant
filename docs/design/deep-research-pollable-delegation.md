# Deep Research Delegation on PollableDelegationService

## Status

Implemented.

## Context

`research` and `research_max` are local `ProcessingService` profiles (`defaults.yaml`) whose model
is one of Google's `deep-research-*` ids. The default assistant's system prompt already tells it to
delegate research work to them via `delegate_to_service`, and `docs/user/USER_GUIDE.md` documents
that async delegations "wake you automatically... you do NOT need to wait for it or poll."

In practice, when the target of an async delegation is a local `ProcessingService`,
`task_worker.py`'s `handle_delegated_profile_run` runs the whole turn inline and blocks a worker for
its entire duration. For a Deep Research profile that duration is the full research run — the LLM
call itself (`GoogleGenAIClient._generate_deep_research_stream`) opens a single long-lived
`interactions.create(background=True, stream=True)` connection and blocks on
`async for chunk in stream` until Google finishes, which can be many minutes (longer for
`research_max`). This is exactly the worker-starvation problem
`docs/design/a2a-native-async-delegation.md` already solved for remote A2A targets via
`PollableDelegationService` (submit once, release the worker, poll on a schedule until terminal).
This change ports Deep Research delegation onto that same machinery instead of adding a second
bespoke async mechanism.

**Scope**: only the `delegate_to_service` path becomes submit+poll. Direct `/research` /
`/research_max` slash-command usage in a live chat keeps today's blocking, real-time-streaming
behavior (thought summaries streamed to the user as they arrive) — that's the desired UX for an
interactive turn and is untouched by this change.

## Design

### 1. Generalized the protocol's error taxonomy

`task_worker.py`'s submit/poll failure classification (transient → keep polling; permanent → fail
fast; not-found → re-submit) previously imported A2A's own exception classes directly
(`a2a.client.A2AClientError` / `A2APermanentError` / `A2ATaskNotFoundError`), even though
`PollableDelegationService` is meant to be provider-agnostic. These moved to
`processing/protocol.py`, generically named, keeping the same subclass chain:

- `DelegationTransientError` (base) — keep polling/retrying.
- `DelegationPermanentError(DelegationTransientError)` — fail fast now.
- `DelegationTaskNotFoundError(DelegationPermanentError)` — re-submit instead of failing.

`a2a/client.py` imports these under its historical names
(`from family_assistant.processing.protocol import DelegationTransientError as A2AClientError`,
etc.) rather than redefining them — they were pure markers with no A2A-specific fields, so this is a
mechanical relocation. `task_worker.py` now only imports the generic names, so it has zero
Google-specific branches. The Deep Research code (below) raises the same generic exceptions
directly.

### 2. Extended `PollableDelegationService.submit_async`

Added two keyword params: `user_name: str` (for `{user_name}` system-prompt templating) and
`db_context: DatabaseContext` (for the resume-chaining lookup, below).
`RemoteA2AService.submit_async` accepts and ignores both. `task_worker.py`'s two call sites pass
`user_name=run["user_name"] or exec_context.user_name` and `db_context=exec_context.db_context`.

### 3. `GoogleGenAIClient`: split "start" from "stream to completion"

The existing `_generate_deep_research_stream`'s input-building logic (trailing user messages +
system prompt + `previous_interaction_id` resolution) was extracted into
`_build_deep_research_create_kwargs`, shared by:

- The unchanged streaming path (`_generate_deep_research_stream`, still used for direct `/research`
  chat).
- `start_deep_research_interaction(messages, *, previous_interaction_id=None)` — calls
  `interactions.create(**kwargs, stream=False)`, returning immediately with the interaction's
  initial (non-terminal) state instead of blocking on the stream. `previous_interaction_id` is
  explicitly overridable (submit_async supplies it from a DB lookup; there's no message history to
  scan for a fresh delegation).
- `get_agent_interaction(interaction_id)` — `interactions.get(id, stream=False)`, one poll.
- `cancel_agent_interaction(interaction_id)` — `interactions.cancel(id)`.

A new `_classify_agent_delegation_error` duck-types on `status_code` (mirroring the existing
`_map_interactions_error` pattern): 404 → `DelegationTaskNotFoundError`, 400/401/403 →
`DelegationPermanentError`, 429/5xx → `DelegationTransientError`. Anything else propagates unwrapped
— `task_worker.py`'s fail-fast default already handles an unrecognized exception correctly with no
extra code (mirrors today's A2A behavior).

`is_deep_research_model` was extracted to a module-level function (the instance method now delegates
to it) so `assistant.py`'s registry setup can use it without an instance.

### 4. `DeepResearchProcessingService(ProcessingService)`

New `processing/deep_research_service.py`. A subclass, constructed in place of a plain
`ProcessingService` only for profiles whose resolved model is a Deep Research model
(`assistant.py`'s per-profile registry loop). This scoping matters: `PollableDelegationService` is a
`runtime_checkable` Protocol, so defining `submit_async`/`poll_async`/`cancel_async` directly on the
base `ProcessingService` would make *every* local profile structurally "pollable," silently
redirecting ordinary delegations onto the poll path.

- `remote_context_id(...)` always returns `None` — Deep Research has no separate context-grouping
  concept; continuation is chained explicitly via `previous_interaction_id`.
- `submit_async`: renders the system prompt via the inherited `_render_system_prompt` (research
  profiles have no context providers, so there's nothing else to aggregate), extracts the delegated
  content as input text via the inherited `_extract_user_content_for_history`, looks up the prior
  completed run for this subconversation (see below) for chaining, and calls
  `start_deep_research_interaction`. Returns a `RemoteSubmission` with the interaction id as
  `remote_task_id`.
- `poll_async`: `in_progress`/`requires_action` → `PENDING`; `completed` →
  `ChatInteractionResult.success(text_reply=interaction.output_text)`; any other terminal status →
  `ChatInteractionResult.error(...)`.
- `cancel_async`: best-effort, swallows and logs (mirrors `RemoteA2AService.cancel_async`).

Like `RemoteA2AService`, this does **not** touch `message_history` — the target service only returns
a `ChatInteractionResult`; posting it to the conversation and notifying is handled generically by
`task_worker._finalize_delegation_run` / `_notify_delegation_if_needed`.

### 5. Resume-chaining

`delegate_to_service`'s `resume_delegation_id` reuses the same `subconversation_id` as the original
run (see `_resolve_resume_subconversation` in `tools/services.py`), and at most one lineage can
occupy a subconversation at a time (enforced by `has_active_run_for_subconversation`).
`DelegationRunsRepository.get_latest_completed_run(conversation_id, subconversation_id, target_service_id)`
finds that prior run unambiguously; `submit_async` uses its stored `remote_task_id` as
`previous_interaction_id` so the new interaction continues the prior one's context server-side, not
just mechanically reusing the subconversation row.

### 6. Registry wiring

`assistant.py`'s per-profile construction loop picks `DeepResearchProcessingService` instead of
`ProcessingService` when `is_deep_research_model(profile_llm_model)` — same constructor arguments,
no `__init__` override needed.

### 7. Config tunability

`ProcessingServiceConfig` (and the `processing_config` YAML model) gained optional
`poll_interval_seconds` / `max_async_seconds`, mirroring `RemoteServiceConfig`'s fields of the same
name; `task_worker.py`'s `_poll_interval_for` / `_max_async_for` already read them via
`getattr(..., default)`, adjusted to also fall back when the field is present but `None` (a
`RemoteServiceConfig` always has a float default; `ProcessingServiceConfig`'s version is optional
since most local profiles aren't pollable). `research_max.max_async_seconds` is set to 7200s in
`defaults.yaml` — the max tier can plausibly run close to or past the worker's default 3600s (1hr)
cap.

**Config-loader gotcha**: `config_loader.py`'s profile-merge step has a `scalar_keys` allowlist for
which `processing_config` fields a profile's own YAML can override over `default_profile_settings`;
new fields must be added there explicitly or a profile-level override is silently dropped during
merge. Both new fields were added to this list.

## Non-goals

- **Direct-chat submit/poll.** `/research` / `/research_max` used interactively keep the existing
  blocking, real-time-streaming turn — that streaming is the point for a live chat session.
- **Server-side `agent_config`/visualization changes.** Unrelated to this change; see
  `gemini-deep-research-tiers.md`.
- **Generalizing beyond Deep Research now.** The installed `google-genai` SDK's `AgentOption`
  already lists other Interactions API agents (e.g. an Antigravity managed agent), reachable through
  the same `interactions.create(agent=..., background=True, ...)` transport. `get_agent_interaction`
  / `cancel_agent_interaction` / `_classify_agent_delegation_error` are named generically (rather
  than `*_deep_research_*`) precisely so a future agent can reuse them as-is — polling/cancelling/
  classifying an interaction by id needs no agent-specific knowledge.
  `start_deep_research_interaction` stays Deep-Research-named because submitting one does need
  agent-specific input shaping (`_build_deep_research_create_kwargs`'s `agent_config`); a second
  agent would get its own `start_*` method and registry predicate (alongside
  `is_deep_research_model`), not a change to this one. No further abstraction (a shared base class,
  a config-driven dispatch table, etc.) is justified until there's a second real implementer to
  generalize from.

## Testing

- `tests/llm/test_google_deep_research.py` — unit tests for `start_deep_research_interaction` /
  `get_agent_interaction` / `cancel_agent_interaction` and the error-classification helper, mocking
  `client.aio.interactions.*`.
- `tests/unit/processing/test_deep_research_service.py` — unit tests for
  `DeepResearchProcessingService.submit_async` / `poll_async` / `cancel_async`, including the
  resume-chaining lookup against a real (test) database.
- `tests/functional/automations/test_async_delegation.py` — an end-to-end case
  (`test_deep_research_delegation_polls_to_completion_and_notifies`) wiring a real
  `DeepResearchProcessingService` through `TaskWorker`'s actual submit-then-poll path (mocking only
  the Google SDK boundary), plus a focused `isinstance` pin
  (`test_deep_research_is_pollable_but_ordinary_local_profile_is_not`) for the exact hazard the
  subclass exists to avoid.
- `tests/functional/web/api/test_a2a_client_integration.py` — updated for the extended
  `submit_async` signature (A2A ignores the two new params).
