# Gemini Antigravity Managed Agent

## Context

Google's Interactions API exposes more than the Deep Research agents we already ride. The same
`client.interactions.create()` transport also serves the **Antigravity managed agent**
(`antigravity-preview-05-2026`): a general-purpose agent that plans and executes multi-step tasks —
reasoning, code execution, file operations, web search and URL reading — inside an isolated,
Google-hosted Linux sandbox. Unlike Deep Research, its reasoning model is chosen by the caller;
`gemini-3.7-flash` is the current default of the Gemini 3.x Flash family it accepts.

[deep-research-pollable-delegation.md](deep-research-pollable-delegation.md) already anticipated
this: the submit/poll/cancel primitives were written generically against "any Interactions API
agent", with only the submit step left agent-specific.

## Decision

Ship the agent as a profile alongside `research`/`research_max` rather than as a tool:

- `antigravity` → agent `antigravity-preview-05-2026` reasoning with `gemini-3.7-flash`, slash
  command `/antigravity`.

Everything that made Deep Research a profile applies unchanged — it is a whole turn handled by a
different provider-side agent, it needs the pollable-delegation path because a run is long, and it
holds no Family Assistant tools. Making it a profile also means the classifier and
`delegate_to_service` can reach it without new machinery.

Under the Rule of Two the profile is **[C] only**: it can act (in its own sandbox, and via the web)
but reaches no household data — `tools_policy` denies everything, and the agent runs server-side
with no Family Assistant tool surface at all. It is not `spawn_worker`, which launches a coding
agent in *our* sandbox; this one is entirely Google-hosted and is reached as a conversation.

## Implementation

1. **`google_genai_client.py`** — the Deep Research path generalises to any Interactions agent:
   - `is_antigravity_model()` (prefix match, tolerant of the client's own `models/` prefix) and
     `is_interactions_agent_model()` join `is_deep_research_model()`.
   - `_build_deep_research_create_kwargs` becomes `_build_agent_create_kwargs`, splitting the shared
     parts (`_extract_agent_input_text`, `_extract_agent_system_prompt`,
     `_resolve_previous_interaction_id`) from the per-agent `agent_config`. Deep Research still
     folds the system prompt into `input`, because it has nowhere else to put it; Antigravity gets a
     first-class `system_instruction`, so the prompt is not read as part of the task.
   - `start_deep_research_interaction` → `start_agent_interaction`; `_generate_deep_research_stream`
     → `_generate_agent_interaction_stream`, with log and error text taking the agent's name from
     the model id. `get_agent_interaction` and `cancel_agent_interaction` were already generic and
     are untouched.
2. **`processing/interactions_agent_service.py`** (was `deep_research_service.py`) —
   `DeepResearchProcessingService` → `InteractionsAgentProcessingService`. No behavioural change:
   submit/poll/cancel and the "no `<turn_context>` block, fold the clock into the prompt" stance
   apply identically to both agents.
3. **`config_models.py`** — new `AntigravityConfig` (`model`, `max_total_tokens`) on
   `ProcessingConfig`. The reasoning model is pinned in `defaults.yaml` rather than left to the API
   default, so an upstream default change is a visible config change rather than a silent behaviour
   change under a prompt users have calibrated.
4. **`assistant.py`** — `is_interactions_agent_model` (not just Deep Research) selects the pollable
   subclass, and `validate_antigravity_profile` rejects two configurations that would otherwise fail
   as plausible-looking answers: `antigravity_config` on a profile that is not the agent (silently
   discarded), and `retry_config` on one that is (a fallback chat model answering from its own
   knowledge instead of running the task).
5. **`google-genai` bumped to `>=2.18.1`** — 2.10.0's `agent_config` union knows only
   `deep-research` and `dynamic`, so an `antigravity` config is rejected client-side before it can
   be sent.

## Non-goals

- **Custom environments.** `environment` (git/GCS/inline sources, network allowlists) and registered
  managed agents via `agents.create()` are left unset, so each run gets a fresh default sandbox.
  Mounting the household's own files into a sandbox would give the profile [B] as well as [C], which
  is a separate decision, not a configuration detail.
- **Surfacing intermediate steps.** Code-execution and file-operation steps are skipped in the
  stream just as Deep Research images are; the transcript stays the agent's prose. Rendering them is
  a UI question we have no requirement for yet.
- **Passing Family Assistant tools as `function` declarations.** The agent supports caller-supplied
  tools; handing it ours would undo the Rule of Two stance above.

## Open questions

- **Reasoning-model tiers.** The agent also accepts `gemini-3.6-flash` and `gemini-3.5-flash-lite`.
  A cheaper sibling profile is a config-only change if it turns out to be wanted; shipping one
  speculatively would just widen the slash-command surface.
- **Wall-clock cap.** `max_async_seconds: 7200` matches `research_max` on the reasoning that an
  autonomous run can be as long as a deep investigation. Whether that is generous or stingy is
  something only real runs will tell us.
