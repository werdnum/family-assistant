---
name: Home Assistant Actions
description: Run Home Assistant actions (formerly "service calls") — turn devices on/off, set thermostats, activate scenes, run scripts, send HA notifications. Activates the discovery and execution tools.
activate_tools:
  - list_home_assistant_actions
  - call_home_assistant_action
---

# Home Assistant Actions

Use this skill any time the user wants you to **do something** in Home Assistant — flip a switch,
dim a light, set a thermostat, lock a door, activate a scene, trigger a script, play media, send an
HA notification, etc. Loading this skill activates two tools:

- `list_home_assistant_actions` — discover what actions exist on the user's HA right now.
- `call_home_assistant_action` — execute one of those actions.

For read-only checks (current sensor value, "is the door closed?", etc.) keep using
`render_home_assistant_template` — you don't need this skill for that.

## Discover before you call

The set of available actions depends entirely on which integrations are installed on the user's HA
instance, and HA itself is a moving target. **Do not guess action names from training data** — they
drift between versions and between installations. Instead, query the live catalog:

1. **Find the entity** if you don't already know it. Use `list_home_assistant_entities` (entity-id
   substring or area filter) to map the user's natural-language target ("the kitchen lamp") to a
   concrete `entity_id` like `light.kitchen`.
2. **Find the action.** The entity's domain prefix (`light.`, `switch.`, `climate.`, `cover.`,
   `media_player.`, `script.`, `scene.`, `notify.`, …) is also the action's `domain`. Call
   `list_home_assistant_actions(domain="light")` to see exactly which actions that domain supports
   on this HA, plus the field schema for each (`brightness_pct`, `color_temp_kelvin`, `transition`,
   …).
3. **Call the action** with
   `call_home_assistant_action(domain=..., action=..., service_data={...})`. Put the entity
   selection in `service_data` as either `entity_id` (string or list) or a `target` block
   (`{"target": {"entity_id": ...}}` / `{"target": {"area_id": ...}}` /
   `{"target": {"device_id": ...}}`).

The catalog is fetched from HA's `GET /api/services` endpoint at call time, so it always matches the
integrations actually installed — no offline list to keep in sync with the HA version.

## Why this matters

- **Action names vary by integration and HA version.** `light.turn_on` is built in, but custom
  integrations register their own (`hue.activate_scene`, `xiaomi_miio.vacuum_clean_zone`, …).
  Discovery is the only reliable way to find them.
- **Field schemas vary too.** A `climate.set_temperature` call wants `temperature` for some
  thermostats and `target_temp_low`/`target_temp_high` for others — the catalog tells you which.
- **Some actions return data.** When the catalog entry has `supports_response: true` (e.g.
  `calendar.get_events`, `weather.get_forecasts`), pass `return_response=true` and read the payload
  off the result.

## Examples

### "Turn on the kitchen light"

```text
list_home_assistant_entities(entity_id_filter="kitchen", area_filter="kitchen")
   → finds light.kitchen_main
list_home_assistant_actions(domain="light", action_filter="turn_on")
   → confirms light.turn_on exists, fields include brightness_pct, transition
call_home_assistant_action(
    domain="light",
    action="turn_on",
    service_data={"entity_id": "light.kitchen_main", "brightness_pct": 75},
)
```

### "Activate movie night"

```text
list_home_assistant_actions(domain="scene")
   → confirms scene.turn_on
call_home_assistant_action(
    domain="scene",
    action="turn_on",
    service_data={"entity_id": "scene.movie_night"},
)
```

### "What's on the family calendar tomorrow?"

```text
list_home_assistant_actions(domain="calendar", action_filter="get_events")
   → calendar.get_events, supports_response=true
call_home_assistant_action(
    domain="calendar",
    action="get_events",
    service_data={"entity_id": "calendar.family", "duration": {"hours": 24}},
    return_response=true,
)
   → read events out of the response payload
```

## Verifying

If the user wants confirmation that an action took effect, follow up with
`render_home_assistant_template` against the relevant entity (e.g.
`{{ states('light.kitchen_main') }}`) — `call_home_assistant_action` already returns the changed
states HA reported, but a template render is the authoritative "what is it now" check.

## What this skill is not for

- Querying state — use `render_home_assistant_template` directly.
- Automations — use `create_automation` (with `automation_type="schedule"` for recurring schedules).
  Automations can themselves *call* actions, but the automation is the right primitive for "do X
  every day" or "do X when Y happens".
- Camera snapshots — `get_camera_snapshot` is its own tool.
