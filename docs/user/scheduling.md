# Scheduling and Task Management Guide

This guide explains how to use the Family Assistant's scheduling features to set reminders, schedule tasks, and automate recurring actions.

## Overview

The Family Assistant provides several ways to schedule future actions:

1. **Reminders** - Simple notifications at specific times
2. **Callbacks** - Assistant wake-ups to continue work or check on tasks
3. **Scheduled Scripts** - Automated script execution at specific times
4. **Recurring Tasks** - Any of the above on a repeating schedule

## Quick Start Examples

### Simple Reminders

```
"Remind me to call the dentist at 3pm"
"Set a reminder for tomorrow morning to review the report"
"Don't let me forget about the meeting at 4:30" (adds follow-up reminders)
```

### Scheduled Callbacks

```
"Check back with me in 2 hours about the project status"
"Continue analyzing this data tomorrow at 9am"
"Wake up at 6pm to see if the download finished"
```

### Script Scheduling

```
"Schedule a script to run at midnight that cleans up old notes"
"Execute a script tomorrow at 9am to summarize all TODO items"
"Run a script in 30 minutes to check system status"
```

### Recurring Tasks

```
"Send me a weather update every morning at 7am"
"Check for new emails every hour"
"Run a cleanup script every Sunday at midnight"
"Remind me to take medication every day at 8am and 8pm"
```

## Detailed Tool Reference

### schedule_reminder

Creates a simple reminder that will notify you at the specified time.

**Parameters:**

- `reminder_time`: When to send the reminder (ISO 8601 format with timezone)
- `message`: The reminder message
- `follow_up`: If true, sends follow-up reminders if you don't respond
- `follow_up_interval`: Time between follow-ups (e.g., "30 minutes", "1 hour")
- `max_follow_ups`: Maximum number of follow-up reminders

**Examples:**

```
"Remind me to submit the report at 5pm"
→ Creates a one-time reminder

"Don't let me forget to take my medication at 8pm"
→ Creates a reminder with follow_up=true for automatic re-reminders
```

### schedule_future_callback

Schedules the assistant to wake up and continue a conversation or check on something.

**Parameters:**

- `callback_time`: When to trigger the callback (ISO 8601 format)
- `context`: Instructions for what the assistant should do when it wakes up

**Examples:**

```
"Check if the server deployment completed in 2 hours"
"Continue our discussion about vacation planning tomorrow morning"
```

### schedule_action

Schedules any type of action (LLM callback or script) for one-time execution.

**Parameters:**

- `schedule_time`: When to execute (ISO 8601 format)
- `action_type`: Either "wake_llm" or "script"
- `action_config`: Configuration for the action
  - For wake_llm: `{"context": "message for assistant"}`
  - For script: `{"script_code": "your Starlark code", "timeout": 600}`

**Examples:**

```
"Schedule a script for tomorrow at noon that counts all notes"
"Run an LLM task at 3pm to review today's activities"
```

### schedule_recurring_task

Creates a recurring LLM callback using RRULE format.

**Parameters:**

- `initial_schedule_time`: When to start (ISO 8601 format)
- `recurrence_rule`: RRULE string defining the schedule
- `callback_context`: What the assistant should do each time
- `description`: Optional identifier for the task

**Examples:**

```
"Send me a daily briefing every morning at 8am"
→ Uses RRULE: "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"

"Check for important emails every Monday and Friday at 9am"
→ Uses RRULE: "FREQ=WEEKLY;BYDAY=MO,FR;BYHOUR=9;BYMINUTE=0"
```

### schedule_recurring_action

Creates a recurring action (LLM callback or script) with more flexibility.

**Parameters:**

- `start_time`: When to start (ISO 8601 format)
- `recurrence_rule`: RRULE string
- `action_type`: Either "wake_llm" or "script"
- `action_config`: Configuration for the action
- `task_name`: Optional identifier

**Examples:**

```
"Run a cleanup script every Sunday at midnight"
→ action_type="script", RRULE="FREQ=WEEKLY;BYDAY=SU;BYHOUR=0;BYMINUTE=0"

"Execute a metrics collection script every 4 hours"
→ action_type="script", RRULE="FREQ=HOURLY;INTERVAL=4"
```

## Managing Scheduled Tasks

### Listing Tasks

```
"Show me all my pending callbacks"
"List all scheduled reminders"
"What tasks are scheduled for tomorrow?"
```

### Modifying Tasks

```
"Change the dentist reminder to 4pm instead"
"Update the context for callback task_123"
```

### Cancelling Tasks

```
"Cancel the reminder about the meeting"
"Stop all daily weather updates"
"Delete all callbacks related to project X"
```

For recurring tasks, each future instance appears as a separate task that can be cancelled individually.

## RRULE Format Guide

RRULE (Recurrence Rule) strings follow the RFC 5545 standard. Common patterns:

### Daily

- Every day: `FREQ=DAILY`
- Every 3 days: `FREQ=DAILY;INTERVAL=3`
- Every weekday: `FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR`

### Weekly

- Every week: `FREQ=WEEKLY`
- Every Monday: `FREQ=WEEKLY;BYDAY=MO`
- Every Monday and Friday: `FREQ=WEEKLY;BYDAY=MO,FR`

### Hourly

- Every hour: `FREQ=HOURLY`
- Every 4 hours: `FREQ=HOURLY;INTERVAL=4`

### Monthly

- Every month: `FREQ=MONTHLY`
- On the 15th of each month: `FREQ=MONTHLY;BYMONTHDAY=15`
- Last day of month: `FREQ=MONTHLY;BYMONTHDAY=-1`

### Time Specification

- At specific time: Add `;BYHOUR=9;BYMINUTE=30` for 9:30 AM
- Multiple times: `;BYHOUR=9,17` for 9 AM and 5 PM

### Limiting Occurrences

- Only 10 times: Add `;COUNT=10`
- Until specific date: Add `;UNTIL=20251231T235959Z`

## Script Automation

Scripts can access various tools and data:

```python
# Example script that runs daily to summarize notes
notes = search_notes("TODO")
summary = []
for note in notes:
    summary.append(f"- {note['title']}")

if summary:
    add_note("Daily TODO Summary", "\n".join(summary))
    wake_llm(f"Found {len(summary)} TODO items today")
```

See the [Scripting Guide](scripting.md) for more details on writing automation scripts.

## Best Practices

1. **Always include timezone** in your time specifications to avoid confusion
2. **Use descriptive task names** for recurring tasks to make them easier to manage
3. **Test RRULE patterns** before creating long-term recurring tasks
4. **Use scripts for deterministic tasks** and LLM callbacks for tasks requiring judgment
5. **Set reasonable intervals** - avoid scheduling tasks too frequently
6. **Monitor the Tasks page** in the web UI to ensure tasks are running as expected

## Troubleshooting

### Task Not Running

- Check timezone settings - ensure times are specified correctly
- Verify the task appears in "Show me pending callbacks"
- Check the Tasks page in web UI for error messages

### Duplicate Tasks

- Recurring tasks create individual instances - this is normal
- Use task names/descriptions to identify related tasks
- Cancel all instances to stop a recurring task completely

### Script Errors

- Scripts have a default 10-minute timeout
- Check script syntax using the assistant before scheduling
- View script execution logs in the Tasks page

## Examples by Use Case

### Personal Productivity

```
"Remind me to review my todo list every morning at 8am"
"Schedule a weekly script to archive completed tasks"
"Alert me 15 minutes before each meeting"
```

### Home Automation

```
"Run a script every night at 10pm to turn off all lights"
"Check if the garage door is open every hour after 9pm"
"Send me a summary of home sensor data every morning"
```

### Information Monitoring

```
"Check for new emails from my boss every 30 minutes during work hours"
"Search for news about my company every morning"
"Monitor specific websites for updates daily"
```

### Health & Wellness

```
"Remind me to take medication at 8am and 8pm with follow-ups"
"Schedule a daily check-in about my mood at 7pm"
"Run a weekly script to summarize my health notes"
```
