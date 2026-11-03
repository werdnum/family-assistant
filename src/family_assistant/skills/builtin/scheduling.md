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

## Choosing a Mechanism

- "Remind me to call mom" → `schedule_reminder`
- "Don't let me forget the meeting" → `schedule_reminder` with `follow_up=true` (use it for
  important reminders too)
- "Check if the download finished in an hour" → `schedule_future_callback`
- "Schedule a script for tomorrow at 9am" → `schedule_action` with `action_type="script"`
- "Send me a weather update every morning" → `create_automation` (`automation_type="schedule"`,
  `action_type="wake_llm"`)
- "Run a script to clean old notes every Sunday" → `create_automation`
  (`automation_type="schedule"`, `action_type="script"`)
- "Stop the daily weather updates" → `list_automations`, then `disable_automation` or
  `delete_automation`

Never build a recurring schedule out of a chain of one-off callbacks.

## Waiting for Something to Happen

To act once a condition holds ("when the download finishes...", "once the update is deployed..."),
check once now. If it doesn't hold yet, `schedule_future_callback` to check again later, and put in
its context the condition, how to check it, the follow-up action and how many checks remain. When
the callback fires, act if the condition holds, otherwise schedule the next check, and give up with
a report after a reasonable number of misses. If what you are waiting for arrives as an event (a
Home Assistant state change, an incoming webhook), an event automation fits better.

## Where Results Land

In the web and iOS apps, when the user has a Home, reminders, callbacks and automations are
delivered to their Home by default, whichever conversation created them. Pass
`conversation="source"` (for automations and `schedule_action`, `"conversation": "source"` in
`action_config`) when the result only makes sense next to the current conversation.
`"conversation": "new"` (web only) starts afresh at every firing, with no history: a new thread of
the user's Home when threads are in use, otherwise a new conversation. In Telegram, results always
arrive in the chat they were created in.

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
- Limit to 10 occurrences: append `;COUNT=10` (counted from creation or the last schedule change,
  including runs that fell while paused)
- Until a date: append `;UNTIL=20251231T235959Z`

## Best Practices

- Use the user's local time when specifying BYHOUR/BYMINUTE — the system handles timezone conversion
- Use descriptive names for automations
- Use scripts for deterministic tasks, wake_llm for tasks requiring judgment
- Manage one-time reminders/callbacks with `list_pending_callbacks`, `modify_pending_callback` and
  `cancel_pending_callback`; manage recurring schedules with `list_automations` or the Automations
  page in the web UI, and stop one with `disable_automation` or `delete_automation` rather than
  cancelling individual runs
- One-shot automations that can no longer fire — a listener waiting on a worker that has finished or
  died, a schedule whose recurrence is exhausted — are deleted by a nightly cleanup, so a missing
  one-shot automation is expected rather than a fault
