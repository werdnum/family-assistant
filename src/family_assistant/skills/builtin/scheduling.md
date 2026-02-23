---
name: Scheduling and Task Management
description: Guide for setting reminders, scheduling callbacks, recurring tasks, and script automations using RRULE format.
---

# Scheduling and Task Management

## Quick Reference

The assistant provides several scheduling mechanisms:

1. **Reminders** - Simple notifications at specific times (`schedule_reminder`)
2. **Callbacks** - Assistant wake-ups to continue work (`schedule_future_callback`)
3. **Scheduled Scripts** - Automated script execution (`schedule_action`)
4. **Recurring Tasks** - Any of the above on a repeating schedule (`schedule_recurring_task`,
   `schedule_recurring_action`)

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

### schedule_recurring_task

- `initial_schedule_time`: ISO 8601
- `recurrence_rule`: RRULE string
- `callback_context`: What to do each time
- `description`: Optional identifier

### schedule_recurring_action

- `start_time`: ISO 8601
- `recurrence_rule`: RRULE string
- `action_type`: "wake_llm" or "script"
- `action_config`: Configuration dict
- `task_name`: Optional identifier

## RRULE Format

Common patterns:

- Every day at 8am: `FREQ=DAILY;BYHOUR=8;BYMINUTE=0`
- Every weekday: `FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR`
- Every Monday and Friday at 9am: `FREQ=WEEKLY;BYDAY=MO,FR;BYHOUR=9;BYMINUTE=0`
- Every 4 hours: `FREQ=HOURLY;INTERVAL=4`
- Monthly on the 15th: `FREQ=MONTHLY;BYMONTHDAY=15`
- Limit to 10 occurrences: append `;COUNT=10`
- Until a date: append `;UNTIL=20251231T235959Z`

## Best Practices

- Always include timezone in time specifications
- Use descriptive task names for recurring tasks
- Use scripts for deterministic tasks, LLM callbacks for tasks requiring judgment
- Manage tasks via "Show me pending callbacks" or the Tasks page in the web UI
