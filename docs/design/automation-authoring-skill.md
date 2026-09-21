# Automation Authoring: Skill Instead of Profile

## Problem

Automations were authored by delegating to a dedicated `automation_creation` processing profile. The
main assistant's system prompt routed every automation request there, and `create_automation`
stamped that profile as the automation's creator provenance.

That stamp is load-bearing at execution time. A `wake_llm` automation carries its stamped profile in
the `llm_callback` payload, and `handle_llm_callback` switches to it before running the woken turn.
So an automation authored through the specialist woke up *inside* the specialist: a profile whose
tool policy is `default_decision: deny` with an allowlist covering automation authoring and nothing
else.

The failure this produces is quiet and total. A weekly "check this website and tell me what it says"
automation wakes into a profile with no browser tools — and no `delegate_to_service` either, so the
woken turn cannot even hand the work to a profile that has them. It reasons about a task it has no
way to perform, produces nothing, and does so again next week.

Two supporting facts made this worse:

- The main assistant's tool policy allowed only the read-only automation tools (`list_automations`,
  `get_automation`, `get_automation_stats`) while its system prompt repeatedly told it to call
  `create_automation`. Delegation was not merely the preferred path, it was the only one available.
- `update_automation` refuses cross-profile updates (it would re-stamp provenance and silently move
  execution authority), so a mis-stamped automation could not be repaired in place from the main
  assistant.

## Why not route wake_llm to a configured profile

The obvious patch is a per-profile `wake_llm_execution_profile_id`, letting `automation_creation`
declare that the automations it writes should wake as `default_assistant`.

That works, but it treats the symptom. The mismatch exists only because the authoring identity and
the intended execution identity are different in the first place, and they are different only
because a specialist profile sits in the middle of a request the main assistant could handle itself.
Removing the intermediary removes the question, and removes a profile from the roster rather than
adding a config field to it.

## Design

`automation_creation` is deleted. Its procedure becomes the **Automation Creation** built-in skill
(`src/family_assistant/skills/builtin/automation-creation.md`), which the main assistant loads on
demand.

The skill's `activate_tools` frontmatter names the automation CRUD and validation tools. Loading a
skill auto-activates those (`llm_loop.py` intercepts skill results and calls
`OnDemandToolsView.activate_tools`), so the tools are absent from the tool surface until an
automation is actually being worked on, and present once it is. The system prompt keeps a one-line
pointer to the skill in place of the ~50 lines of automation guidance it used to carry.

Two configuration changes make that reachable:

1. The automation CRUD tools plus `test_event_listener` are added to
   `tools_config.on_demand_local_tools`. `activate_tools` only ever reveals on-demand tools, so a
   tool that is not on-demand cannot be activated by a skill.
2. The same tools are added to the default profile's `tools_policy` allow rules. Activation
   re-checks policy before marking a tool active, so a policy grant is required as well.
3. And to `complex_tasks`. A profile's own `tools_policy` *replaces* `default_profile_settings`
   rather than merging with it, so a grant on the default profile does not reach a profile that
   defines its own policy. `complex_tasks` is the documented route for automations the main
   assistant finds hard, so without repeating the grant there the skill would load under `/complex`
   with nothing it could call.

Scheduled automations created this way are stamped `default_assistant` and wake with the full tool
set, including `delegate_to_service` — the woken turn delegates the browsing (or research, or media
work) it needs at wake time.

Event automations are unchanged by this: a `wake_llm` event listener still routes to `event_handler`
regardless of who authored it, because the triggering event is untrusted (see
[Corrected invariant](#corrected-invariant) below). The skill states that limit and its allowed tool
set explicitly, so the authoring model scopes an event wake to what `event_handler` can reach rather
than assuming the capabilities a scheduled wake gets.

### Security

The marginal capability granted to the default profile is negligible. It is already full-trust [BC]
under the Rule of Two, and already holds `schedule_action` (which enqueues one-off `wake_llm`
actions) and `execute_script` (whose scripts can call `wake_llm()`). Adding `create_automation` lets
it schedule recurring work it could already schedule as one-offs.

The capability-monotonicity property from [automation_provenance.md](automation_provenance.md) is
preserved: a script still executes under the profile that authored it, so a restricted profile still
cannot author a script that reaches tools it lacks. What changed is which profile authors, not
whether authorship confers authority.

`ops_automation` is unaffected. It keeps `allow_wake_llm: false` and its `create_automation`
`argument_equals: {action_type: wake_llm}` deny rule, so it remains script-only and confined.

### Migration

Execution-time profile resolution is deliberately fail-loud: an automation stamped with a profile
that no longer resolves raises rather than downgrading to the default profile, because a script
validated for one profile's tools and visibility must not run under another's.

Deleting the profile without touching stored rows would therefore convert every existing
`automation_creation`-owned automation from "wakes uselessly" into "raises on every fire". Migration
`restamp_automation_creation` re-stamps them to `default_assistant` in both `event_listeners` and
`schedule_automations`.

For `wake_llm` rows this restores the intended behaviour. For `script` rows it widens the tool set
the script is validated and executed against, from the authoring profile's to the main assistant's —
an accepted trade-off, since both are full-trust [BC] and `default_assistant` is a superset of what
`automation_creation` could reach.

The migration is not reversible: rows it re-stamps are indistinguishable afterwards from rows the
main assistant stamped itself.

## Corrected invariant

Comments in `actions.py` and `task_worker.py` stated that `wake_llm` always runs under the task
worker's default trusted profile and never honors the stored profile. That stopped being true when
`handle_llm_callback` gained fail-loud profile resolution; the comments were left behind, and they
describe the exact assumption whose violation caused this bug.

The accurate rule:

- Schedule automations, `schedule_action`, and a script's `wake_llm()` stamp their originating
  profile, and `handle_llm_callback` resolves and switches to it.
- **Event listeners** are the exception: `EventProcessor` stamps `event_handler` regardless of
  origin, because the triggering event is untrusted content and the woken LLM is the injectable
  component.
- `allow_wake_llm=False` therefore guards a real escalation on the event-listener path, and on the
  stamped path prevents enqueueing a wake that `handle_llm_callback`'s execution-time re-check would
  reject at fire time anyway. Refusing at creation is better than either outcome.

## Consequences

- `/automate` is gone. Automation requests are handled in the ordinary conversation.
- Automation authoring runs on the main assistant's model (50 iterations) rather than the
  specialist's Sonnet 5 at 100 iterations, and shares the conversation's iteration budget. Genuinely
  hard authoring can still go through `/complex`, which is also full-trust and whose stamped
  automations likewise wake with full tools.
- Validation tool-call noise now appears in the user's conversation rather than an isolated
  delegation.

## Testing

`tests/functional/tools/test_builtin_skill_tool_activation.py` asserts, for **every** built-in skill
with `activate_tools`, that each named tool exists, is on-demand, and is advertisable under the
default profile's policy. A skill naming a tool that fails any of those silently teaches the model
to reach for something it will never receive — the same class of defect as the original bug.

`tests/functional/storage/test_restamp_automation_creation_migration.py` covers the re-stamp,
including that rows owned by other profiles and legacy unstamped rows are left alone.
