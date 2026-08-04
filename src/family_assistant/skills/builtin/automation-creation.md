---
name: Automation Creation
description: Procedure for building event-based and schedule-based automations that work on the first try — validate the trigger against real data, test the action, then create. Activates the automation management and testing tools.
activate_tools:
  - create_automation
  - update_automation
  - enable_automation
  - disable_automation
  - delete_automation
  - list_automations
  - get_automation
  - get_automation_stats
  - test_event_listener
  - query_recent_events
  - execute_script
  - test_script_with_simulated_tools
---

# Automation Creation

Load this skill whenever the user wants something to happen automatically — on a schedule, or when
an event occurs — and whenever they want to inspect, change, or remove an automation they already
have. Loading it activates the automation management tools plus the tools for testing a trigger or
action before you commit to it.

**Create automations yourself.** Do not delegate automation creation to another profile. An
automation records the profile that created it, and a `wake_llm` automation wakes into that profile
— so an automation created inside a narrow specialist wakes with that specialist's narrow tool set
and cannot do the work. Creating it here means it wakes as the main assistant, with the full tool
set and the ability to delegate at wake time.

Two consequences worth knowing:

- An automation owned by a different profile cannot be updated from here. `update_automation` fails
  with an ownership error naming the owning profile. Recreate it here instead.
- Whatever the woken turn will need, it needs at wake time, not now. If a weekly check involves
  browsing a website, the wake context should say so — the woken turn delegates to the browser
  profile itself.

## Procedure

The goal is an automation that works without manual intervention. Validate assumptions with tools
rather than guessing; the cost of a wrong entity ID or a mistaken event shape is a silent weekly
failure that nobody notices for a month.

### 1. Understand the request, and look at what exists

Parse what should happen, and when. Then call `list_automations` to see whether something similar
already exists — an existing automation that already fires correctly is the best available template,
and `get_automation_stats` tells you whether it actually runs. Ask the user if the request is
ambiguous.

### 2. Work out the trigger

**Schedule automations** need an RRULE and a timezone. Times in an RRULE are interpreted in the
user's configured timezone, not UTC — `BYHOUR=9` means 9am local, and DST is handled for you. The
Scheduling skill covers RRULE patterns in detail.

**Event automations** need an event source and a filter. Never guess the shape of an event: call
`query_recent_events` and read a real one.

```
query_recent_events(source_id="home_assistant", hours=24, limit=10)
```

Simple equality conditions go in `match_conditions`, using exact field names with dot notation for
nested fields (`new_state.state`). Anything else — a genuine state transition, a numeric threshold,
a pattern across entities — needs a `condition_script`, a Python expression receiving `event` and
returning a boolean.

Then confirm the filter would actually have matched:

```
test_event_listener(
    source="home_assistant",
    match_conditions={"entity_id": "sensor.motion", "new_state.state": "on"},
    hours=24,
    limit=5,
)
```

A filter that matches nothing in recent history is the single most common way an automation ends up
silently dead. If it matches nothing, find out why before creating it.

For Home Assistant entity IDs and states, load the Home Assistant skill and check the entity
directly rather than trusting a name from the conversation.

### 3. Choose the action type

Use `action_type="script"` when the response is fixed and deterministic — logging, a notification
with known content, data collection. Scripts run immediately, cost nothing per run, and behave
identically every time.

Use `action_type="wake_llm"` when the response needs judgement — when what to do depends on reading
the situation. A script can also call `wake_llm()` conditionally, which is often the best of both:
cheap deterministic filtering, waking only when something is actually worth the user's attention.

Prefer a script where a script suffices.

### 4. Test the action before you attach it

For scripts, test the code in isolation first: `test_script_with_simulated_tools` when you want tool
calls mocked, `execute_script` when running them for real is safe. Feed it the event structure you
confirmed in step 2, not an invented one. Check the edge cases that actually occur — a missing
field, a `None`, an `unavailable` state — and write defensively with `.get()` and explicit defaults.
The Scripting guide (`get_user_documentation_content` on `scripting.md`) has the full API.

For `wake_llm`, read your own context back as if you were the woken turn, which has none of this
conversation. Does it say what happened, and what to do about it? "Check the Bopple site for
Messina's dark chocolate cake and tell me if it's available" is actionable. "Check this" is not.

### 5. Create it, then tell the user how to see it

Call `create_automation` with the validated parameters. Then give the user the automation ID, a link
to it in the web interface (`/automations/event/<id>` or `/automations/schedule/<id>` under the
server URL in your context), what will trigger it, and how they'll know it worked.

## Managing existing automations

`list_automations` to find, `get_automation` for full configuration, `get_automation_stats` for run
history — the last-run timestamp is the fastest way to tell a broken automation from a quiet one.
`enable_automation` / `disable_automation` to pause without losing the definition, and
`delete_automation` to remove.

To stop a recurring automation, disable or delete the automation itself. Do not try to cancel its
individual queued instances.

## Common patterns

Zone entry — a real transition, not merely being home:

```python
old_state = event.get("old_state", {}).get("state", "")
new_state = event.get("new_state", {}).get("state", "")
return old_state != "home" and new_state == "home"
```

Threshold crossing — screen out non-numeric states first, then compare as a float so 25.9 counts:

```python
new = event.get("new_state", {}).get("state")
if new in (None, "unavailable", "unknown"):
    return False
return float(new) > 25
```

Pattern matching across entities:

```python
return event.get("entity_id", "").startswith("sensor.motion_") and (
    event.get("new_state", {}).get("state") == "on"
)
```

## Rate limits

Event automations have a daily execution limit (5 by default). Design conditions to filter noise at
the trigger rather than relying on the limit to do it.
