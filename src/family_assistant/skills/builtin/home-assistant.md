---
name: Home Assistant
description: Talk to the user's Home Assistant — read state, render templates, list entities, see camera snapshots, query history, and execute actions (formerly "service calls"). Activates the full Home Assistant tool set.
activate_tools:
  - render_home_assistant_template
  - list_home_assistant_entities
  - list_home_assistant_actions
  - call_home_assistant_action
  - get_camera_snapshot
  - download_state_history
---

# Home Assistant

Load this skill any time the user wants you to **read from or act on** their Home Assistant instance
— query a sensor, check whether a door is closed, look up history, see a camera, flip a switch, set
a thermostat, activate a scene, trigger a script, etc. Loading the skill activates six tools:

| Tool                             | Use it for                                                  |
| -------------------------------- | ----------------------------------------------------------- |
| `render_home_assistant_template` | Read current state via Jinja2 (`{{ states('...') }}`, etc.) |
| `list_home_assistant_entities`   | Discover entity IDs by substring or area                    |
| `list_home_assistant_actions`    | Discover available actions and their field schemas          |
| `call_home_assistant_action`     | Execute an action (a.k.a. service call)                     |
| `get_camera_snapshot`            | Pull a still image from a HA camera entity                  |
| `download_state_history`         | Fetch historical state changes for entities                 |

For **schedules and event-driven automations**, use the automations framework (`create_automation`)
— automations can themselves call HA actions, but the automation is the right primitive for "every
day at 9pm" or "when X happens".

## Discover before you guess

The set of entities, actions, and field schemas depends entirely on which integrations are installed
on the user's HA, and HA itself is a moving target. **Do not guess names from training data** — they
drift between versions and between installations. Instead, query the live catalog at runtime:

1. **Find the entity** with `list_home_assistant_entities` (substring or area filter). The user says
   "the kitchen lamp"; you map that to a concrete `entity_id` like `light.kitchen_main`.
2. **For state queries**, use `render_home_assistant_template` directly:
   `{{ states('light.kitchen_main') }}` or
   `{{ state_attr('climate.living_room', 'current_temperature') }}`.
3. **For actions**, look up the schema with `list_home_assistant_actions(domain="light")` to see
   what fields the action accepts (`brightness_pct`, `color_temp_kelvin`, `transition`, …) and
   whether it supports a response payload.
4. **Call** with `call_home_assistant_action(domain=..., action=..., service_data={...})`. Put the
   entity selection in `service_data` as either `entity_id` (string or list) or a `target` block
   (`{"target": {"entity_id": ...}}` / `{"target": {"area_id": ...}}` /
   `{"target": {"device_id": ...}}`).

The action catalog is fetched live from HA's `GET /api/services` at call time, so it always matches
the integrations actually installed — no offline list to keep in sync with the HA version.

## Why discovery matters

- **Action and field names vary by integration and HA version.** `light.turn_on` is built in, but
  custom integrations register their own (`hue.activate_scene`, `xiaomi_miio.vacuum_clean_zone`, …).
  The catalog is the only reliable way to find them.
- **Field shapes vary too.** A `climate.set_temperature` call wants `temperature` for some
  thermostats and `target_temp_low`/`target_temp_high` for others — the catalog tells you which.
- **Some actions return data.** When the catalog entry has `supports_response: true` (e.g.
  `calendar.get_events`, `weather.get_forecasts`), pass `return_response=true` and read the payload
  off the result.

## Examples

### "Is the front door locked?"

```text
list_home_assistant_entities(entity_id_filter="front_door")
   → finds lock.front_door
render_home_assistant_template(template="{{ states('lock.front_door') }}")
   → "locked"
```

### "Turn on the kitchen light at 75%"

```text
list_home_assistant_entities(entity_id_filter="kitchen", area_filter="kitchen")
   → finds light.kitchen_main
list_home_assistant_actions(domain="light", action_filter="turn_on")
   → confirms light.turn_on, fields include brightness_pct, transition
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

### "Show me the doorbell camera"

```text
list_home_assistant_entities(entity_id_filter="camera")
   → finds camera.front_door
get_camera_snapshot(camera_entity_id="camera.front_door")
   → image attached to the reply
```

### "How has the bedroom temperature changed today?"

```text
download_state_history(entity_ids=["sensor.bedroom_temperature"])
   → JSON attachment with state changes; pass to a chart tool to plot
```

## Verifying actions

`call_home_assistant_action` already returns the changed states HA reported, but a follow-up
`render_home_assistant_template` is the authoritative "what is it now" check.

## What this skill is not for

- Recurring or event-triggered behavior — use `create_automation` (the automation can itself call HA
  actions).
- Other camera backends (Reolink/Frigate) — those have their own dedicated tools (`list_cameras`,
  `get_camera_frame`, …).
