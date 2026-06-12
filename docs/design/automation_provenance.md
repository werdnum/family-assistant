# Automation Provenance: Profile-Consistent Script Validation and Execution

## Problem

Automation scripts were **validated** against one processing profile but **executed** under a
different one, so a script could pass validation at creation time and then behave differently (or
fail outright) when it ran.

- **Validation** (`create_automation_tool` in `tools/automations.py`) generated tool stubs from the
  tools provider of whatever profile was *creating* the automation (typically `automation_creation`
  or `default_assistant`).
- **Execution** (`handle_script_execution` in `task_worker.py`) always used
  `exec_context.processing_service`, which the task worker builds from its **default** processing
  service. The docstring claimed scripts ran under the restricted `event_handler` profile, but no
  profile switch actually happened — unlike `handle_llm_callback`, which switches to the `reminder`
  profile via the processing services registry.

The result: the tool set the validator checked against was not the tool set available at runtime.

## Security model

The injection threat for a deterministic **script** action is limited to *data flow*: an attacker
who controls the triggering event can influence the *arguments* at the call sites the author wired
up, but cannot change *which* tools are called. That is much weaker than injecting an LLM, so it is
proportionate to run a script with the authority of the profile (and user) that created it:

> If the agent can do X, it is fine to let it schedule X to happen later.

This also makes capability **monotone** through the create→execute chain: a script created under a
restricted profile can never gain access to tools that profile lacks. (A restricted profile that
could itself author automations would otherwise be a privilege‑escalation path.)

`wake_llm` actions are different: the woken LLM reads attacker-influenced event content and *is* the
injectable component. Those intentionally keep running under the restricted `event_handler` profile
and are **not** affected by this change.

## Design

Capture the creating profile (and user) on the automation, thread it through to the script execution
task, and resolve it at execution time:

1. **Schema** — `event_listeners` and `schedule_automations` gain nullable `processing_profile_id`
   and `created_by_user_id` columns (migration `add_automation_provenance`).
2. **Capture** — `create_automation_tool` stores `exec_context.processing_profile_id` and
   `exec_context.user_id`. `update_automation_tool` re-validates a changed inline script and
   re-stamps the provenance with the *updating* profile/user, so the updater's authority governs.
   The web automations API stamps the default profile and the authenticated web user the same way
   (re-stamping on updates that change the action config), and the `schedule_action` tool threads
   the acting profile/user into one-time scheduled script payloads via `execute_action`.
3. **Thread** — the `script_execution` task payload carries `processing_profile_id` (and
   `created_by_user_id`). For schedule automations this flows through `_build_script_payload`; for
   event automations and one-time scheduled actions it flows through `execute_action`.
4. **Resolve** — `handle_script_execution` resolves the recorded profile via the processing services
   registry (`_resolve_script_execution_service`) and re-points the execution context (tools
   provider, visibility grants, note labels) at it. Validation, which already uses the creating
   profile's tools provider, is therefore consistent with execution by construction.

### Stored scripts

Automations can reference a stored script by `script_name` instead of inline code. Stored scripts
are global and validated against the tools of the profile that *saved* them, while an automation
referencing one executes it under the *automation creator's* profile — so creating or updating such
an automation re-validates the stored script's code against the creating profile's tools
(`validate_action_scripts_with_provider`), using the automation runtime globals plus the script's
declared and supplied parameters as inputs. A profile that lacks a tool the stored script uses is
rejected at the boundary rather than failing at runtime.

### Fallback

Legacy automations created before provenance was tracked have a `NULL` profile and fall back to the
task worker's default profile (the previous behaviour). An automation explicitly stamped with a
non-default profile that can no longer be resolved (renamed/removed profile, or a non-local one)
fails loudly instead of downgrading: the script was validated for that profile's tools and
visibility, so running it under a different policy could change its capabilities.

## Confirm-gated tools

A profile's policy may mark some tools `confirm` (e.g. `delete_calendar_event`). Scripts execute in
the task worker with no interactive channel, so confirm-gated tool calls are routed through the
existing durable confirmation machinery: a confirmation request is created and delivered to the
automation's owner, and the script receives a "pending approval — not run yet" result. The tool runs
later, after the user approves, via the `confirmation_tool_execution` task. This mirrors the email
intake profile, which already defers confirm-gated tools the same way.
