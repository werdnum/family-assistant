---
name: Scheduling and Task Management
description: Guide for setting reminders, scheduling one-time callbacks and scripts, and creating recurring schedules via the automations framework.
---

# Scheduling and Task Management

## Quick Reference

The assistant provides several scheduling mechanisms:

1. **Reminders** - Simple notifications at specific times (`schedule_reminder`)
2. **Callbacks** - Assistant wake-ups to continue work (`schedule_future_callback`)
3. **Scheduled Scripts/Actions** - One-time action execution (`schedule_action`)
4. **Recurring Schedules** - Use the automations framework (`create_automation` with
   `automation_type="schedule"`) for anything that should repeat.

## Tools

### schedule_reminder

- `reminder_time`: ISO 8601 with timezone
- `message`: The reminder text
- `follow_up`: If true, re-reminds if no response
- `follow_up_interval`: e.g., "30 minutes", "1 hour"
- `max_follow_ups`: Maximum follow-up count

### schedule_future_callback

- `callback_time`: ISO 8601
- `context`: Instructions for what to do when waking up

### schedule_action

- `schedule_time`: ISO 8601
- `action_type`: "wake_llm" or "script"
- `action_config`: `{"context": "..."}` for wake_llm, `{"script_code": "...", "timeout": 600}` for
  script

### create_automation (for recurring schedules)

For anything that should repeat, create a schedule automation:

- `name`: Short identifier for the automation
- `automation_type`: `"schedule"`
- `trigger_config`: `{"recurrence_rule": "<RRULE>", "timezone": "<IANA tz>"}`
- `action_type`: `"wake_llm"` or `"script"`
- `action_config`: `{"context": "..."}` for wake_llm, `{"script_code": "..."}` or
  `{"script_name": "..."}` for script

Manage recurring schedules with `list_automations`, `get_automation`, `enable_automation`,
`disable_automation`, and `delete_automation`.

## RRULE Format

**Times in RRULE strings are always interpreted in the user's configured timezone.** For example, if
the user's timezone is `Australia/Sydney`, `BYHOUR=9` means 9:00 AM Sydney time. You do NOT need to
convert to UTC — the system handles that automatically and correctly across DST transitions.

Common patterns:

- Every day at 8am: `FREQ=DAILY;BYHOUR=8;BYMINUTE=0`
- Every weekday at 7am: `FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0`
- Every Monday and Friday at 9am: `FREQ=WEEKLY;BYDAY=MO,FR;BYHOUR=9;BYMINUTE=0`
- Every 4 hours: `FREQ=HOURLY;INTERVAL=4`
- Monthly on the 15th: `FREQ=MONTHLY;BYMONTHDAY=15`
- Limit to 10 occurrences: append `;COUNT=10`
- Until a date: append `;UNTIL=20251231T235959Z`

## Best Practices

- Use the user's local time when specifying BYHOUR/BYMINUTE — the system handles timezone conversion
- Use descriptive names for automations
- Use scripts for deterministic tasks, wake_llm for tasks requiring judgment
- Manage one-time reminders/callbacks via `list_pending_callbacks`; manage recurring schedules via
  `list_automations` or the Automations page in the web UI
