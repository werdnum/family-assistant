# Family Assistant Scripting Guide

This guide provides a reference for assistants to write Starlark scripts when responding to user
requests for automation and complex operations.

**Important**: Scripts are primarily a tool for assistants to fulfill user requests, not for direct
user interaction. Before writing scripts, understand Starlark's limitations and available APIs to
write effective scripts.

## Starlark Language Overview

Starlark is a dialect of Python designed for configuration and deterministic execution. While it
looks like Python, it has important differences:

### Key Differences from Python

01. **No exceptions or try-except**: Errors terminate the script. Check inputs carefully.
02. **No while loops**: Use for loops and recursion instead.
03. **No generators or yield**: All functions return complete values.
04. **Limited standard library**: Only basic functions are available.
05. **No isinstance()**: Use `type(x) == type("")` for type checking.
06. **Immutable globals**: After initial evaluation, global variables cannot be changed.
07. **No implicit string concatenation**: Use `+` explicitly (`"hello" + "world"`).
08. **No chained comparisons**: Write `1 < x and x < 5` instead of `1 < x < 5`.
09. **Integer limits**: 32-bit signed integers only (no arbitrary precision).
10. **Dictionary iteration order**: Guaranteed to be deterministic.

### Restricted Top-Level Statements

- `for` loops and `if` statements must be inside functions at the top level
- Global variables cannot be reassigned after definition
- Use list comprehensions for top-level data generation

For full Starlark syntax details, see the
[official Starlark documentation](https://github.com/bazelbuild/starlark/blob/master/spec.md).

## Available APIs

### Tools API

**Note**: The available tools depend on your profile's permissions. To see all available tools and
their parameters, ask the assistant to list them or check the tool reference documentation.

All Family Assistant tools are available in scripts through two interfaces:

#### 1. Direct Callable Interface (Recommended)

```starlark

# Call tools directly as functions
result = add_or_update_note(title="Meeting Notes", content="...")
emails = search_emails(query="project update")
events = get_calendar_events(days_ahead=7)

```

#### 2. Functional Interface

```starlark

# List all available tools
tools = tools_list()
for tool in tools:
    print(tool["name"] + ": " + tool["description"])

# Get detailed info about a tool
tool_info = tools_get("send_email")
print(tool_info["parameters"])

# Execute a tool by name
result = tools_execute("add_or_update_note",
    title="Shopping List",
    content="Milk, Eggs, Bread")

# Execute with JSON arguments
args_json = '{"to": "user@example.com", "subject": "Test"}'
result = tools_execute_json("send_email", args_json)

```

### Attachment API

Scripts can create and manipulate attachments (files, images, charts, etc.) that are automatically
propagated back to the assistant and shown to the user.

#### Understanding Attachment Objects

When you create or receive attachments in scripts, they are represented as **dictionaries** with
metadata fields:

```starlark
# Example attachment dict structure
{
    "id": "550e8400-e29b-41d4-a716-446655440000",  # UUID for referencing
    "filename": "chart.png",                        # Original filename
    "mime_type": "image/png",                       # Content type
    "size": 1024,                                   # Size in bytes
    "description": "Temperature chart"              # Human-readable description
}
```

You can access fields directly: `attachment["id"]`, `attachment["filename"]`, etc.

#### Creating Attachments

Use `attachment_create()` to create new attachments from script-generated content:

```starlark
# Create a text file attachment
data_file = attachment_create(
    content="Temperature readings: 72, 75, 73, 71",
    filename="temp_data.txt",
    description="Temperature sensor data",
    mime_type="text/plain"
)

# Last expression is returned, so this attachment is sent to the assistant
data_file
```

```starlark
# Create a CSV file attachment
csv_content = "date,temperature,humidity\n2024-01-01,72,45\n2024-01-02,75,48"
csv_file = attachment_create(
    content=csv_content,
    filename="sensor_data.csv",
    description="Daily sensor readings",
    mime_type="text/csv"
)
csv_file
```

**Parameters:**

- `content` (bytes or str): File content (strings are UTF-8 encoded automatically)
- `filename` (str): Filename for the attachment
- `description` (str, optional): Human-readable description
- `mime_type` (str, optional): MIME type (default: "application/octet-stream")

**Returns:** Dictionary with attachment metadata (id, filename, mime_type, size, description)

#### Working with Tool-Returned Attachments

Many tools return attachments (charts, reports, images, etc.). These come in two forms:

**Single attachment (no text):**

```starlark
# Tool that returns just an attachment
chart = create_vega_chart(
    spec='{"mark": "line", ...}',
    data_attachments=[data_file]
)

# chart is a dict with: id, filename, mime_type, size, description
print("Created chart: " + chart["filename"])
chart  # Return it to make it visible to the assistant
```

**Attachment(s) with text:**

```starlark
# Tool that returns text and attachments
result = process_documents(query="invoices")

# result is a dict with: {"text": "...", "attachments": [{...}, {...}]}
print(result["text"])  # "Found 2 invoices"

# Access attachments
for att in result["attachments"]:
    print("- " + att["filename"] + " (" + att["mime_type"] + ")")
```

#### Returning Multiple Attachments

Return a list to send multiple attachments to the assistant:

```starlark
# Create multiple charts
chart1 = create_vega_chart(spec=temperature_spec, data_attachments=[temp_data])
chart2 = create_vega_chart(spec=humidity_spec, data_attachments=[humidity_data])

# Return both as a list
[chart1, chart2]
```

```starlark
# Mix manual and tool-created attachments
data_file = attachment_create(
    content="Raw data: 1,2,3",
    filename="raw.txt",
    mime_type="text/plain"
)

report = generate_report(title="Analysis Report")

# Return both
[data_file, report]
```

#### Functional Composition with Attachments

One of the most powerful patterns is passing attachments from one tool to another:

```starlark
# Process data, then visualize - all in one expression
chart = create_vega_chart(
    spec='{"mark": "bar", "encoding": {...}}',
    data_attachments=[
        jq_query(raw_data_attachment, '.[] | select(.value > 10)')
    ]
)
chart
```

```starlark
# Multi-step data pipeline
# 1. Query and filter data
filtered_data = jq_query(
    source_attachment,
    '.items[] | select(.category == "temperature")'
)

# 2. Transform the filtered data
transformed = jq_query(
    filtered_data,
    'map({date: .timestamp, value: .reading})'
)

# 3. Create visualization
chart = create_vega_chart(
    spec='{"mark": "line", ...}',
    data_attachments=[transformed]
)

chart  # Final chart is sent to assistant
```

#### Practical Examples

##### Example 1: Generate CSV Report

```starlark
# Query notes and generate CSV report
def create_task_report():
    result_str = search_notes(query="TODO")
    notes = json_decode(result_str) if result_str else []

    if len(notes) == 0:
        return "No tasks found"

    # Build CSV content
    csv = "title,created,status\n"
    for note in notes:
        csv += note["title"] + ","
        csv += note.get("created_at", "unknown") + ","
        csv += "pending\n"

    # Create attachment
    report = attachment_create(
        content=csv,
        filename="tasks.csv",
        description="Task report with " + str(len(notes)) + " items",
        mime_type="text/csv"
    )

    return report

create_task_report()
```

##### Example 2: Data Visualization Pipeline

```starlark
# Fetch data, transform it, and create a chart
def visualize_temperature_trend(days=7):
    # Get calendar events (example data source)
    events_str = get_calendar_events(days_ahead=days)
    events = json_decode(events_str) if events_str else []

    # Transform to JSON array for chart
    data = []
    for event in events:
        if "temp" in event.get("summary", "").lower():
            data.append({
                "date": event["start"],
                "temperature": 72  # Would extract from event
            })

    # Create data attachment
    data_json = json_encode(data)
    data_file = attachment_create(
        content=data_json,
        filename="temp_data.json",
        description="Temperature data for past " + str(days) + " days",
        mime_type="application/json"
    )

    # Create chart from data
    chart_spec = json_encode({
        "mark": "line",
        "encoding": {
            "x": {"field": "date", "type": "temporal"},
            "y": {"field": "temperature", "type": "quantitative"}
        }
    })

    chart = create_vega_chart(
        spec=chart_spec,
        data_attachments=[data_file]
    )

    # Return chart (data_file not returned, chart includes it)
    return chart

visualize_temperature_trend(7)
```

##### Example 3: Multiple Related Files

```starlark
# Generate multiple related reports
def generate_weekly_reports():
    # Create summary
    summary = attachment_create(
        content="Weekly Summary\nTotal events: 42\nCompleted: 38",
        filename="summary.txt",
        mime_type="text/plain"
    )

    # Create detailed CSV
    csv_content = "day,events,completed\nMon,10,9\nTue,8,8\n..."
    details = attachment_create(
        content=csv_content,
        filename="details.csv",
        mime_type="text/csv"
    )

    # Return both files
    return [summary, details]

generate_weekly_reports()
```

##### Example 4: Tool Attachment Passthrough

```starlark
# Use attachment IDs to pass between tools
def analyze_and_chart(query):
    # Get data from one tool
    data_result = jq_query(source_data, query)

    # data_result is a dict with attachment info
    print("Filtered data: " + data_result["filename"])

    # Pass the attachment to chart tool using the data dict directly
    # The tool will extract the ID automatically
    chart = create_vega_chart(
        spec=chart_spec,
        data_attachments=[data_result]  # Pass the whole dict
    )

    return chart

analyze_and_chart('.[] | select(.value > 100)')
```

#### Best Practices

1. **Always provide descriptive filenames**: Use meaningful names like "temperature_report.csv"
   instead of "data.csv"

2. **Set appropriate MIME types**: This helps tools and the assistant handle attachments correctly

3. **Use functional composition**: When possible, chain tools together rather than creating
   intermediate attachments

4. **Return attachments as the last expression**: The last expression in your script is what gets
   sent to the assistant

5. **Access fields safely**: Use `.get()` when working with tool results:
   `result.get("attachments", [])`

#### Important Notes

- **Automatic propagation**: Any attachment dict returned from your script (as the final expression)
  is automatically sent to the assistant with the correct metadata
- **Nested lists**: You can return `[[att1, att2], att3]` - all attachments are extracted
  automatically
- **Tool compatibility**: Tools that accept attachment IDs can receive attachment dicts directly -
  they'll extract the ID automatically
- **Storage**: Attachments are stored and tracked - the assistant can reference them in future
  messages
- **Size limits**: Check your configuration for attachment size limits (typically 100MB max)

### JSON Functions

```starlark
# Encode Python objects to JSON
data = {"tasks": ["review PR", "update docs"]}
json_str = json_encode(data)

# Decode JSON strings
parsed = json_decode('{"name": "test", "value": 42}')
```

### Time API Functions

A comprehensive time API is available for working with dates, times, and timezones:

#### Creating Time Objects

```starlark
# Get current time
now = time_now()  # Local timezone
now_utc = time_now_utc()  # UTC timezone

# Create specific time
meeting_time = time_create(
    year=2024, month=3, day=15,
    hour=14, minute=30, second=0,
    timezone_name="America/New_York"
)

# Parse from string
date = time_parse("2024-03-15 14:30", "%Y-%m-%d %H:%M", "UTC")

# From Unix timestamp
timestamp_time = time_from_timestamp(1710515400, 0)
```

#### Formatting and Timezones

```starlark
# Format time as string
formatted = time_format(now, "%Y-%m-%d %H:%M:%S")
date_only = time_format(now, "%Y-%m-%d")

# Convert timezone
la_time = time_in_location(now, "America/Los_Angeles")

# Check timezone validity
if timezone_is_valid("Europe/London"):
    london_time = time_in_location(now, "Europe/London")
```

#### Time Components

```starlark
# Extract components
year = time_year(now)
month = time_month(now)
day = time_day(now)
hour = time_hour(now)
minute = time_minute(now)
second = time_second(now)
weekday = time_weekday(now)  # 0=Monday, 6=Sunday

# Check conditions
if is_weekend(now):
    print("It's the weekend!")

if is_between(9, 17, now):  # Between 9 AM and 5 PM
    print("Business hours")
```

#### Time Arithmetic

```starlark
# Add seconds
tomorrow = time_add(now, DAY)  # DAY = 86400 seconds
next_hour = time_add(now, HOUR)  # HOUR = 3600 seconds

# Add duration with units
future = time_add_duration(now, 3, "days")
meeting_end = time_add_duration(meeting_time, 90, "minutes")

# Calculate difference
diff_seconds = time_diff(future, now)
```

#### Time Comparisons

```starlark
# Compare times
if time_before(now, meeting_time):
    print("Meeting hasn't started yet")

if time_after(now, deadline):
    print("Deadline has passed")

if time_equal(t1, t2):
    print("Times are identical")
```

#### Duration Handling

```starlark
# Parse duration strings
duration = duration_parse("2h30m")  # Returns seconds: 9000

# Convert to human-readable
human = duration_human(3665)  # Returns: "1h1m5s"

# Duration constants available
# SECOND = 1, MINUTE = 60, HOUR = 3600, DAY = 86400, WEEK = 604800
```

#### Practical Examples

```starlark
# Schedule something for next business day
def next_business_day():
    next_day = time_add(time_now(), DAY)
    while is_weekend(next_day):
        next_day = time_add(next_day, DAY)
    return next_day

# Check if event is soon
def is_event_soon(event_time, threshold_minutes=30):
    now = time_now()
    if time_before(now, event_time):
        diff = time_diff(event_time, now)
        return diff <= threshold_minutes * MINUTE
    return False

# Format relative time
def relative_time_str(target_time):
    now = time_now()
    diff = abs(time_diff(target_time, now))
    
    if diff < MINUTE:
        return "just now"
    elif diff < HOUR:
        return str(int(diff / MINUTE)) + " minutes ago"
    elif diff < DAY:
        return str(int(diff / HOUR)) + " hours ago"
    else:
        return time_format(target_time, "%Y-%m-%d")
```

### Global Variables

Scripts can receive global variables when executed:

```starlark

# These might be available depending on context
# user_email, user_name, current_date, etc.
if "user_email" in globals():
    send_email(to=user_email, subject="Reminder", body="...")

```

## Security Model

- **Tool Access**: Scripts only have access to tools allowed by the current profile
- **Sandboxing**: No file system, network, or system access
- **Timeout**: Scripts timeout after 10 minutes (to allow for external API calls)
- **No Imports**: Cannot import external code

## Common Patterns

### 1. Search and Summarize Notes

```starlark
def summarize_project_notes(project_name):
    result_str = search_notes(query=project_name)
    notes = json_decode(result_str) if result_str else []

    if len(notes) == 0:
        return "No notes found for " + project_name

    summary = "Found " + str(len(notes)) + " notes for " + project_name + ":\n\n"
    for note in notes:
        summary += "- " + note["title"] + "\n"

    # Create summary note
    add_or_update_note(
        title=project_name + " Summary",
        content=summary
    )
    return summary

# Execute
summarize_project_notes("Project Alpha")

```

### 2. Process TODOs

```starlark
def collect_todos():
    # Search for TODO items
    result_str = search_notes(query="TODO")
    notes = json_decode(result_str) if result_str else []

    todos = []
    for note in notes:
        if "TODO" in note.get("content", ""):
            todos.append(note["title"])

    if len(todos) > 0:
        # Create consolidated TODO list
        content = "# Active TODOs\n\n"
        for todo in todos:
            content += "- [ ] " + todo + "\n"

        add_or_update_note(
            title="TODO List - " + str(len(todos)) + " items",
            content=content
        )

    return {"count": len(todos), "items": todos}

collect_todos()

```

### 3. Calendar-Based Automation

```starlark
def create_meeting_prep_notes():
    # Get upcoming events
    events = get_calendar_events(days_ahead=1)

    for event in events:
        if "meeting" in event.get("summary", "").lower():
            # Create prep note for each meeting
            add_or_update_note(
                title="Prep: " + event["summary"],
                content="Meeting at " + event["start"] + "\n\nAgenda:\n- \n\nNotes:\n"
            )

    return "Created prep notes for " + str(len(events)) + " meetings"

create_meeting_prep_notes()

```

### 4. Email Digest

```starlark
def create_email_digest(search_term):
    # Search recent emails
    result_str = search_emails(query=search_term)
    emails = json_decode(result_str) if result_str else []

    if len(emails) == 0:
        return "No emails found"

    digest = "# Email Digest: " + search_term + "\n\n"
    for email in emails[:10]:  # Limit to 10 most recent
        digest += "**From**: " + email.get("sender", "Unknown") + "\n"
        digest += "**Subject**: " + email.get("subject", "No subject") + "\n"
        digest += "---\n\n"

    add_or_update_note(
        title="Email Digest - " + search_term,
        content=digest
    )

    return "Created digest with " + str(len(emails)) + " emails"

create_email_digest("project update")

```

### 5. Conditional Actions

```starlark
def smart_reminder(title, check_calendar=True):
    # Check if reminder already exists
    existing = search_notes(query=title)
    notes = json_decode(existing) if existing else []

    if len(notes) > 0:
        print("Reminder already exists")
        return

    # Check calendar if requested
    content = "Reminder: " + title
    if check_calendar:
        events = get_calendar_events(days_ahead=7)
        related = [e for e in events if title.lower() in e.get("summary", "").lower()]
        if len(related) > 0:
            content += "\n\nRelated events:\n"
            for event in related:
                content += "- " + event["summary"] + " at " + event["start"] + "\n"

    add_or_update_note(title="Reminder: " + title, content=content)
    return "Reminder created"

smart_reminder("Team standup", check_calendar=True)

```

## Event-Triggered Scripts

Scripts can now be automatically triggered by events from Home Assistant, document indexing, and
other sources. This enables powerful automation without the delay and cost of LLM processing.

### Creating Event-Triggered Scripts

Ask the assistant to create a script-based event automation:

```
"Create a script that logs all motion events when motion is detected in the living room"
"Run a script to send me a Telegram message when the temperature exceeds 25°C"
"Set up a script to track energy usage whenever the meter reading changes"
```

### Advanced Event Matching with Condition Scripts

For complex event matching that can't be expressed as simple equality checks, use condition scripts:

```
"Create an automation that detects when someone arrives home (state changes from not 'home' to 'home')"
"Alert me when temperature rises above 25°C but only if it was below 20°C before"
"Watch for any sensor that starts with 'sensor.motion_' and turns on"
```

Condition scripts are Starlark expressions that:

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

Note: Starlark doesn't have `float()`, only `int()`. For decimal values like "25.5", the safest
approach is:

- Truncate to integer: `int(event.get('new_state', {}).get('state', '0').split('.')[0])` (e.g.,
  "25.5" becomes 25, "26" stays 26)
- Be aware this loses precision - a value of "25.9" will not trigger a `> 25` condition

### Event Script Context

When triggered by an event, scripts receive special global variables:

```starlark
# Available global variables in event scripts:
# event - Dictionary containing all event data
# conversation_id - The conversation this automation belongs to
# automation_id - ID of the event automation that triggered this script

# Example: Log temperature changes (note: Starlark only has int(), not float())
temp = int(event.get("new_state", {}).get("state", "0").split('.')[0])  # Truncate decimals
old_temp = int(event.get("old_state", {}).get("state", "0").split('.')[0]) if event.get("old_state") else 0

add_or_update_note(
    title="Temperature Log - " + time_format(time_now(), "%Y-%m-%d"),
    content=time_format(time_now(), "%H:%M") + " - " + str(temp) + "°C (was " + str(old_temp) + "°C)\n",
    append=True  # If available
)
```

### Example Event Scripts

#### Motion Logging

```starlark
# Log all motion events with timestamps
def log_motion():
    entity = event.get("entity_id", "unknown")
    timestamp = event.get("timestamp", time_format(time_now(), "%Y-%m-%d %H:%M:%S"))
    
    add_or_update_note(
        title="Motion Log - " + time_format(time_now(), "%Y-%m-%d"),
        content="Motion detected: " + entity + " at " + timestamp + "\n",
        append=True
    )
    return "Motion logged"

log_motion()
```

#### Temperature Alerts

```starlark
# Alert on high temperature during business hours
def check_temperature():
    temp = int(event.get("new_state", {}).get("state", "0").split('.')[0])  # Truncate decimals
    hour = time_hour(time_now())
    
    if temp > 25 and hour >= 9 and hour < 18:
        send_telegram_message(
            message="🌡️ High temperature alert: " + str(temp) + "°C in " + event.get("entity_id", "unknown")
        )
        return "Alert sent"
    return "Temperature OK"

check_temperature()
```

#### Document Processing

```starlark
# Process newly indexed documents
def process_document():
    if event.get("source_id") != "indexing":
        return "Not an indexing event"
    
    metadata = event.get("metadata", {})
    doc_type = metadata.get("type", "unknown")
    
    if doc_type == "email" and "invoice" in metadata.get("subject", "").lower():
        # Extract and log invoice information
        add_or_update_note(
            title="Invoice Log",
            content="New invoice from: " + metadata.get("sender", "Unknown") + "\n",
            append=True
        )
        send_telegram_message(message="📧 New invoice received")
    
    return "Document processed"

process_document()
```

#### Energy Usage Tracking

```starlark
# Track hourly energy usage
def track_energy():
    reading = int(event.get("new_state", {}).get("state", "0").split('.')[0])  # Truncate decimals
    hour_str = time_format(time_now(), "%Y-%m-%d %H:00")
    
    # Log the reading
    add_or_update_note(
        title="Energy Log - " + time_format(time_now(), "%Y-%m-%d"),
        content=hour_str + ": " + str(reading) + " kWh\n",
        append=True
    )
    
    # Alert on high usage
    if reading > 5.0:
        send_telegram_message(
            message="⚡ High energy usage: " + str(reading) + " kWh"
        )
    
    return "Energy tracked"

track_energy()
```

### Security and Limitations

Event scripts run with the `event_handler` profile which has restricted tools:

- Can read/write notes
- Can send Telegram messages (not emails, to prevent spam)
- Can read documents and calendar events
- Cannot delete data or control devices (to prevent automation loops)
- Cannot delegate to other services

Scripts have a 10-minute timeout to support long-running operations but should aim to complete
quickly.

### Testing Event Scripts

Before creating an event listener, test your script:

```
"Test this event script with a sample temperature event: [paste your script]"
"Validate this script syntax: [paste your script]"
```

### Managing Script Automations

```
"Show me all my script-based event automations"
"Disable the temperature monitoring script"
"Convert my motion automation from wake_llm to a script"
"Create a script that uses wake_llm when the garage door opens"
```

## Important Notes

### wake_llm Function

The `wake_llm` function allows scripts to wake the LLM with custom context when certain conditions
are met. This is particularly useful in event-triggered scripts where you want to provide the LLM
with specific information about what happened.

```starlark
# Wake the LLM with custom context
wake_llm(context, include_event=True)
```

**Parameters:**

- `context` (str or dict): Either a simple string message or a dictionary of key-value pairs to
  provide to the LLM as context
  - **String (recommended for simple messages)**: When you just need to send a message, pass a
    string directly
  - **Dictionary**: For structured data with multiple fields
- `include_event` (bool, optional): Whether to include the original event data in the wake context
  (default: True)

**Usage in Event Scripts:**

```starlark
# Example: Simple string message (recommended for straightforward alerts)
temp = int(event.get("new_state", {}).get("state", "0").split('.')[0])  # Truncate decimals
if temp > 30:
    wake_llm("High temperature alert: " + str(temp) + "°C detected in " + event.get("entity_id", "unknown"))

# Example: Using string for motion detection
if event.get("new_state", {}).get("state") == "on":
    wake_llm("Motion detected in " + event.get("entity_id", "unknown"))
```

```starlark
# Example: Dictionary for complex context with multiple fields
temp = int(event.get("new_state", {}).get("state", "0").split('.')[0])  # Truncate decimals
if temp > 30:
    wake_llm({
        "alert": "High temperature detected",
        "temperature": temp,
        "location": event.get("entity_id", "unknown"),
        "suggestion": "Consider turning on the AC"
    })
```

```starlark
# Example: Process important emails with structured data
if event.get("source_id") == "indexing":
    metadata = event.get("metadata", {})
    if metadata.get("type") == "email" and "urgent" in metadata.get("subject", "").lower():
        wake_llm({
            "urgent_email": True,
            "from": metadata.get("sender"),
            "subject": metadata.get("subject"),
            "action_needed": "Please review this urgent email"
        }, include_event=False)  # Don't include full event details
```

**Important Notes:**

- The wake_llm function can be called multiple times in a script
- Each call adds to a queue of wake contexts that will be processed
- The LLM will receive all contexts when the script completes
- When passing a string, it's automatically converted to `{"message": "your string"}`
- Use meaningful keys in your context dictionary for clarity when using dict format

### Currently Not Available

- **StateAPI**: No persistent storage between script runs
- **File/Network Access**: Scripts are sandboxed with no filesystem or network access
- **Module Imports**: Cannot import external code
- **Random Numbers**: No random number generation for determinism

### Working with Tool Results

- Most tools return JSON strings - use `json_decode()` to parse them
- Some tools return simple strings or numbers directly
- Check for empty results before parsing

### Error Handling

```starlark
# Starlark has no try/except, so check inputs carefully
result_str = search_notes(query="test")
if result_str and result_str != "[]":
    notes = json_decode(result_str)
else:
    notes = []

# Check for None values
if event.get("new_state") and event.get("new_state", {}).get("state"):
    value = int(event.get("new_state", {}).get("state", "0").split('.')[0])  # Truncate decimals
else:
    value = 0
```

### Performance Tips

- Scripts timeout after 10 minutes (but try to keep them efficient)
- Avoid processing very large datasets in memory
- Limit search results when possible
- Use specific queries to reduce result sets
- Be mindful of external API rate limits when making many tool calls

## Assistant Guidelines for Scripts

### When to Use Scripts

Use scripts when users request:

- Complex automation with multiple steps
- Data processing and transformation
- Conditional logic based on search results
- Scheduled or event-triggered automation
- Batch operations across multiple items

### Script Development Best Practices

1. **Understand the request**: Clarify what the user wants to achieve
2. **Check available tools**: Use `tools_list()` if unsure what's available
3. **Handle edge cases**: Check for empty results, invalid data
4. **Test first**: For complex scripts, test key operations separately
5. **Provide feedback**: Use print() to show progress for long operations

### Common User Requests and Script Patterns

#### Summarize my TODO notes

```starlark
# Search, process, and create summary
results = search_notes(query="TODO")
if results and results != "[]":
    notes = json_decode(results)
    # Process and summarize...
```

#### Send me daily reminders

```starlark
# Check calendar and create contextual reminders
events = get_calendar_events(days_ahead=1)
for event in events:
    # Create reminder logic...
```

#### Monitor temperature and alert me

```starlark
# For event-triggered scripts
if int(event.get("new_state", {}).get("state", "0").split('.')[0]) > 25:  # Truncate decimals
    send_telegram_message(message="High temperature alert!")
```

### Explaining Scripts to Users

When presenting scripts to users:

- Focus on what the script does, not how
- Mention any limitations or requirements
- Suggest testing before creating event automations
- Offer to modify if it doesn't meet their needs
