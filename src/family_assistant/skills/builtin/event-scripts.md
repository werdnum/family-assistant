---
name: Event-Triggered Scripts
description: Guide for creating event-triggered automations with scripts — condition scripts, event context variables, wake_llm, and practical examples for motion, temperature, energy, and document events.
activate_tools:
  - execute_script
  - create_automation
  - list_automations
  - get_automation
  - enable_automation
  - disable_automation
  - delete_automation
---

# Event-Triggered Scripts

Scripts can be automatically triggered by events from Home Assistant, document indexing, and other
sources. This enables powerful automation without the delay and cost of LLM processing.

## Creating Event-Triggered Scripts

Ask the assistant to create a script-based event automation:

```
"Create a script that logs all motion events when motion is detected in the living room"
"Run a script to send me a Telegram message when the temperature exceeds 25C"
"Set up a script to track energy usage whenever the meter reading changes"
```

## Advanced Event Matching with Condition Scripts

For complex event matching that can't be expressed as simple equality checks, use condition scripts:

```
"Create an automation that detects when someone arrives home (state changes from not 'home' to 'home')"
"Alert me when temperature rises above 25C but only if it was below 20C before"
"Watch for any sensor that starts with 'sensor.motion_' and turns on"
```

Condition scripts are Python expressions that:

- Receive the full event data in the `event` variable
- Must return a boolean value (True to trigger, False to ignore)
- Can access nested fields with `.get()` to handle missing data safely
- Support complex logic with `and`, `or`, and `not` operators

Common patterns:

- Zone entry:
  `event.get('old_state', {}).get('state') != 'home' and event.get('new_state', {}).get('state') == 'home'`
- Temperature threshold: `int(event.get('new_state', {}).get('state', '0').split('.')[0]) > 25`
- Entity pattern matching: `event.get('entity_id', '').startswith('sensor.motion_')`
- Attribute changes:
  `event.get('old_state', {}).get('attributes', {}).get('battery') != event.get('new_state', {}).get('attributes', {}).get('battery')`

Note: For decimal values like "25.5", you can use `float()` for precise comparisons:

- `float(event.get('new_state', {}).get('state', '0'))` converts "25.5" to 25.5
- You can also truncate to integer with `int()` if precision isn't needed

## Event Script Context

When triggered by an event, scripts receive special global variables:

```python
# Available global variables in event scripts:
# event - Dictionary containing all event data
# conversation_id - The conversation this automation belongs to
# automation_id - ID of the event automation that triggered this script

temp = int(event.get("new_state", {}).get("state", "0").split(".")[0])
old_temp = (
    int(event.get("old_state", {}).get("state", "0").split(".")[0])
    if event.get("old_state")
    else 0
)

add_or_update_note(
    title="Temperature Log - " + time_format(time_now(), "%Y-%m-%d"),
    content=time_format(time_now(), "%H:%M")
    + " - "
    + str(temp)
    + "C (was "
    + str(old_temp)
    + "C)\n",
    append=True,
)
```

## Example Event Scripts

### Motion Logging

```python
def log_motion():
    entity = event.get("entity_id", "unknown")
    timestamp = event.get("timestamp", time_format(time_now(), "%Y-%m-%d %H:%M:%S"))
    add_or_update_note(
        title="Motion Log - " + time_format(time_now(), "%Y-%m-%d"),
        content="Motion detected: " + entity + " at " + timestamp + "\n",
        append=True,
    )
    return "Motion logged"


log_motion()
```

### Temperature Alerts

```python
def check_temperature():
    temp = int(event.get("new_state", {}).get("state", "0").split(".")[0])
    hour = time_hour(time_now())
    if temp > 25 and hour >= 9 and hour < 18:
        send_telegram_message(
            message="High temperature alert: "
            + str(temp)
            + "C in "
            + event.get("entity_id", "unknown")
        )
        return "Alert sent"
    return "Temperature OK"


check_temperature()
```

### Document Processing

```python
def process_document():
    if event.get("source_id") != "indexing":
        return "Not an indexing event"
    metadata = event.get("metadata", {})
    doc_type = metadata.get("type", "unknown")
    if doc_type == "email" and "invoice" in metadata.get("subject", "").lower():
        add_or_update_note(
            title="Invoice Log",
            content="New invoice from: " + metadata.get("sender", "Unknown") + "\n",
            append=True,
        )
        send_telegram_message(message="New invoice received")
    return "Document processed"


process_document()
```

### Energy Usage Tracking

```python
def track_energy():
    reading = int(event.get("new_state", {}).get("state", "0").split(".")[0])
    hour_str = time_format(time_now(), "%Y-%m-%d %H:00")
    add_or_update_note(
        title="Energy Log - " + time_format(time_now(), "%Y-%m-%d"),
        content=hour_str + ": " + str(reading) + " kWh\n",
        append=True,
    )
    if reading > 5.0:
        send_telegram_message(message="High energy usage: " + str(reading) + " kWh")
    return "Energy tracked"


track_energy()
```

## wake_llm Function

The `wake_llm` function allows scripts to wake the LLM with custom context when certain conditions
are met. This is particularly useful in event-triggered scripts where you want to provide the LLM
with specific information about what happened.

```python
wake_llm(context, include_event=True)
```

**Parameters:**

- `context` (str or dict): Either a simple string message or a dictionary of key-value pairs
  - **String (recommended for simple messages)**: Pass a string directly
  - **Dictionary**: For structured data with multiple fields
- `include_event` (bool, optional): Whether to include the original event data (default: True)

**Usage examples:**

```python
# Simple string message (recommended for straightforward alerts)
temp = int(event.get("new_state", {}).get("state", "0").split(".")[0])
if temp > 30:
    wake_llm(
        "High temperature alert: "
        + str(temp)
        + "C detected in "
        + event.get("entity_id", "unknown")
    )
```

```python
# Dictionary for complex context with multiple fields
temp = int(event.get("new_state", {}).get("state", "0").split(".")[0])
if temp > 30:
    wake_llm({
        "alert": "High temperature detected",
        "temperature": temp,
        "location": event.get("entity_id", "unknown"),
        "suggestion": "Consider turning on the AC",
    })
```

```python
# Process important emails with structured data
if event.get("source_id") == "indexing":
    metadata = event.get("metadata", {})
    if (
        metadata.get("type") == "email"
        and "urgent" in metadata.get("subject", "").lower()
    ):
        wake_llm(
            {
                "urgent_email": True,
                "from": metadata.get("sender"),
                "subject": metadata.get("subject"),
                "action_needed": "Please review this urgent email",
            },
            include_event=False,
        )
```

**Notes:**

- `wake_llm` can be called multiple times in a script
- Each call adds to a queue of wake contexts processed when the script completes
- When passing a string, it's automatically converted to `{"message": "your string"}`
- Use meaningful keys in context dictionaries for clarity

## Security and Limitations

Event scripts run with the `event_handler` profile which has restricted tools:

- Can read/write notes
- Can send Telegram messages (not emails, to prevent spam)
- Can read documents and calendar events
- Cannot delete data or control devices (to prevent automation loops)
- Cannot delegate to other services

Scripts have a 10-minute timeout but should aim to complete quickly.

## Testing Event Scripts

Before creating an event listener, test your script:

```
"Test this event script with a sample temperature event: [paste your script]"
"Validate this script syntax: [paste your script]"
```

## Managing Script Automations

```
"Show me all my script-based event automations"
"Disable the temperature monitoring script"
"Convert my motion automation from wake_llm to a script"
"Create a script that uses wake_llm when the garage door opens"
```

## Failure Notifications

When an automation script fails after exhausting all retry attempts, the assistant is automatically
notified with details about the error, including the script code and triggering event data. The
assistant will then summarize the failure and suggest fixes.

To disable failure notifications for a specific automation, set `notify_on_failure: false` in the
automation's `action_config`.
