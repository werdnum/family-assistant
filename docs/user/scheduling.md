# Scheduling and Task Management Guide

This guide explains how to set one-off reminders, schedule assistant callbacks and scripts, and
manage recurring schedules via the automations framework.

## Overview

1. **Reminders (`schedule_reminder`)** — Simple notifications at a specific time (with optional
   follow-ups if you don't reply).
2. **Callbacks (`schedule_future_callback`)** — Wake the assistant later to continue work or check
   on something. If nothing needs your attention when it wakes, it finishes quietly without
   messaging you.
3. **One-off Scheduled Actions (`schedule_action`)** — Run a one-time `wake_llm` or Python `script`
   at a specific time.
4. **Recurring Schedules (`create_automation` with `automation_type="schedule"`)** — Anything that
   repeats on an RRULE schedule. Never build a recurring task out of chained one-off callbacks. See
   [automations.md](automations.md).

### Where they arrive

With threads turned on (see [threads.md](threads.md)), reminders, callbacks, and automations created
in the web or iOS app arrive in your **Home** by default — except when created inside a thread,
where they stay with that thread. Ask for "here" or "in this conversation" (`conversation="source"`)
to keep one in the originating conversation. In Telegram, they always reply in the chat where they
were created. See [automations.md](automations.md#where-the-assistant-replies).

## Tool Reference

### `schedule_reminder`

- `reminder_time`: ISO 8601 timestamp with timezone
- `message`: Reminder message
- `follow_up`: If `True`, sends follow-up reminders until you respond (skipped automatically if the
  assistant can see you have already dealt with it)
- `follow_up_interval`: Interval between follow-ups (e.g. `"30 minutes"`, `"1 hour"`)
- `max_follow_ups`: Maximum follow-up reminders
- `conversation`: `"home"` or `"source"` (defaults to the rule under
  [Where they arrive](#where-they-arrive))

### `schedule_future_callback`

- `callback_time`: ISO 8601 timestamp with timezone
- `context`: Instructions for what the assistant should check or do when woken
- `conversation`: `"home"` or `"source"`

### `schedule_action`

- `schedule_time`: ISO 8601 timestamp with timezone
- `action_type`: `"wake_llm"` or `"script"`
- `action_config`: `{"context": "..."}` for `wake_llm`, or `{"script_code": "...", "timeout": 600}`
  for `script`

### Recurring schedules (`create_automation`)

- `name`: Short identifier for the automation
- `automation_type`: `"schedule"`
- `trigger_config`: `{"recurrence_rule": "<RRULE>", "timezone": "<IANA tz>"}`
- `action_type`: `"wake_llm"` or `"script"`
- `action_config`: `{"context": "..."}` for `wake_llm`, or `{"script_code": "..."}` (inline) /
  `{"script_name": "..."}` (stored script) for `script`. See [scripting.md](scripting.md).

## Managing Scheduled Tasks

- **One-off reminders and callbacks:** List (`list_pending_callbacks`), reschedule or edit context
  (`modify_pending_callback`), or cancel (`cancel_pending_callback`) in conversation or on the
  **Tasks** page in the web UI.
- **Recurring schedules:** List (`list_automations`), update (`update_automation`), pause/resume
  (`disable_automation` / `enable_automation`), or remove (`delete_automation`) in conversation or
  on the **Automations** page. To stop a recurring schedule, disable or delete the automation itself
  rather than cancelling individual queued instances.

## RRULE Format Guide

RRULE strings follow RFC 5545, and **`BYHOUR`/`BYMINUTE` are always interpreted in the user's
configured timezone** (DST transitions are handled automatically):

- **Daily / Weekday:** `FREQ=DAILY;BYHOUR=8;BYMINUTE=0` or
  `FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0`
- **Weekly:** `FREQ=WEEKLY;BYDAY=MO,FR;BYHOUR=9;BYMINUTE=0`
- **Hourly:** `FREQ=HOURLY;INTERVAL=4`
- **Monthly:** `FREQ=MONTHLY;BYMONTHDAY=15` (or `BYMONTHDAY=-1` for the last day of the month)
- **Multiple times per day:** `;BYHOUR=9,17;BYMINUTE=0`
- **Limiting occurrences:** Append `;COUNT=10` (at most `10000`) or `;UNTIL=20251231T235959Z`.
  `COUNT` starts from the first run after creation or the last schedule edit; runs that fall while
  the automation is paused still count toward `COUNT`, so a paused automation can exhaust its count
  before it is resumed.
