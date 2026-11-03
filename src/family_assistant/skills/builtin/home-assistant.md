---
name: Home Assistant
description: Check and control anything in the home — lights, vacuum, heating and AC, locks, blinds, pool, appliances, scenes, scripts, sensors, cameras and their history.
activate_tools:
  - list_home_assistant_entities
  - list_home_assistant_actions
  - call_home_assistant_action
  - render_home_assistant_template
  - get_camera_snapshot
  - download_state_history
---

# Home Assistant

Load this whenever the user wants to read from or act on a device in the home. Most of what they ask
for is here even if you have not seen the device mentioned. `get_home_status` is a fixed overview of
a few things, so a device missing from it tells you nothing: search for it.

Names and fields vary by installation, so look them up rather than guessing:

1. **Find it:** `list_home_assistant_entities(query="kitchen lamp")`. Results carry current state,
   which often answers a "what is X?" question outright. Use `area_filter` for a room.
2. **Check the action:** `list_home_assistant_actions(domain="light", action_filter="turn_on")`
   shows the fields it accepts.
3. **Call it:**
   `call_home_assistant_action(domain="light", action="turn_on", service_data={"entity_id": "light.kitchen_main", "brightness_pct": 75})`.
   The result reports the states that changed.

For an attribute or a calculation, use `render_home_assistant_template`. When a catalog entry has
`supports_response: true`, pass `return_response=true` and read the payload from the result.

Check the household's notes for specifics, such as room names, zones or a device's commands. For
anything recurring or triggered by an event, use an automation instead.
