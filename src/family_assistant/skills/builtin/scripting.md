---
name: Python Scripting
description: Reference for writing Python scripts in the sandboxed Monty engine — sandbox constraints, available APIs (tools, attachments, JSON, base64, LLM, time), common patterns, and best practices.
activate_tools:
  - execute_script
  - save_script
  - list_scripts
  - get_script
  - delete_script
---

# Python Scripting Reference

Scripts run real Python code in a sandboxed Monty engine. This guide covers the sandbox constraints,
available APIs, and common patterns for writing effective scripts.

## Sandbox Constraints

1. **No imports**: Cannot import external modules (except heavily sandboxed os, sys, typing,
   pathlib)
2. **No file/network access**: Scripts are sandboxed with no filesystem or network access
3. **Limited builtins**: Only safe built-in functions are available
4. **Resource limits**: Scripts have memory (256MB) and recursion depth (100) limits
5. **Timeout**: Scripts timeout after 10 minutes

### Sandbox Limitations

Some Python features are **not available** in the Monty engine:

#### Structural Limitations

- **No class definitions**: You cannot define classes using the `class` keyword
- **No generators**: No `yield` statements or generator functions
- **No match/case statements**: Pattern matching is not supported
- **No context managers**: No `with` statements (use try/finally instead)
- **No del statement**: Cannot explicitly delete variables

#### Syntax Limitations

- **No dict unpacking in literals**: `{**d1, **d2}` syntax is not supported (use
  `dict1.update(dict2)` or manual merging)
- **No str.format() method**: Use f-strings instead (`f"Hello {name}"` not
  `"Hello {}".format(name)`)
- **No map/filter builtins**: Use list comprehensions instead (`[x*2 for x in items]` not
  `map(lambda x: x*2, items)`)

#### Buggy or Limited Features

- **Decorators are buggy**: Avoid using decorators on functions
- **Exception arguments**: Exceptions only accept 0 or 1 string arguments (`raise ValueError("msg")`
  works, `raise ValueError("msg", data)` does not)

#### What IS Supported

Despite these limitations, the following features **do work**:

- **Exception handling**: `try`/`except`/`finally` blocks work normally
- **While loops**: `while` loops are fully supported
- **All numeric types**: `float()`, `int()`, and decimal math work correctly
- **Set types**: `set()` and `frozenset()` are available
- **For loops and comprehensions**: All iteration constructs work normally
- **Function definitions**: You can define and call functions normally

### Working Around Limitations

**Instead of classes, use functions and dicts:**

```python
def create_counter():
    return {"count": 0}


def increment_counter(counter):
    counter["count"] += 1
    return counter


counter = create_counter()
increment_counter(counter)
```

**Instead of dict unpacking, use update():**

```python
merged = dict1.copy()
merged.update(dict2)
```

**Instead of str.format(), use f-strings:**

```python
f"Hello {name}"
```

**Instead of map/filter, use comprehensions:**

```python
[x * 2 for x in items]
[x for x in items if x > 10]
```

**Instead of context managers, use try/finally:**

```python
resource = acquire_resource()
try:
    do_something(resource)
finally:
    release_resource(resource)
```

## Available APIs

### Tools API

All Family Assistant tools are available in scripts through two interfaces:

#### Direct Callable Interface (Recommended)

```python
result = add_or_update_note(title="Meeting Notes", content="...")
emails = search_emails(query="project update")
events = get_calendar_events(days_ahead=7)
```

#### Functional Interface

```python
tools = tools_list()
for tool in tools:
    print(tool["name"] + ": " + tool["description"])

tool_info = tools_get("send_email")
print(tool_info["parameters"])

result = tools_execute(
    "add_or_update_note", title="Shopping List", content="Milk, Eggs, Bread"
)

args_json = '{"to": "user@example.com", "subject": "Test"}'
result = tools_execute_json("send_email", args_json)
```

**Note**: The available tools depend on the current profile's permissions.

### Attachment API

Scripts can create and manipulate attachments (files, images, charts) that are automatically
propagated back to the assistant and shown to the user.

#### Understanding Attachment Objects

Attachments are dictionaries with metadata fields:

```python
{
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "filename": "chart.png",
    "mime_type": "image/png",
    "size": 1024,
    "description": "Temperature chart",
}
```

#### Creating Attachments

```python
data_file = attachment_create(
    content="Temperature readings: 72, 75, 73, 71",
    filename="temp_data.txt",
    description="Temperature sensor data",
    mime_type="text/plain",
)
data_file  # Last expression is returned to the assistant
```

**Parameters:**

- `content` (bytes or str): File content (strings are UTF-8 encoded automatically)
- `filename` (str): Filename for the attachment
- `description` (str, optional): Human-readable description
- `mime_type` (str, optional): MIME type (default: "application/octet-stream")

#### Working with Tool-Returned Attachments

**Single attachment (no text):**

```python
chart = create_vega_chart(spec='{"mark": "line", ...}', data_attachments=[data_file])
print("Created chart: " + chart["filename"])
chart  # Return it to make it visible
```

**Attachment(s) with text:**

```python
result = process_documents(query="invoices")
print(result["text"])
for att in result["attachments"]:
    print("- " + att["filename"] + " (" + att["mime_type"] + ")")
```

#### Returning Multiple Attachments

```python
chart1 = create_vega_chart(spec=temperature_spec, data_attachments=[temp_data])
chart2 = create_vega_chart(spec=humidity_spec, data_attachments=[humidity_data])
[chart1, chart2]
```

#### Functional Composition with Attachments

```python
chart = create_vega_chart(
    spec='{"mark": "bar", "encoding": {...}}',
    data=jq_query(raw_data_attachment, ".[] | select(.value > 10)"),
)
chart
```

```python
filtered_data = jq_query(
    source_attachment, '.items[] | select(.category == "temperature")'
)
transformed = jq_query(filtered_data, "map({date: .timestamp, value: .reading})")
chart = create_vega_chart(spec='{"mark": "line", ...}', data=transformed)
chart
```

#### Attachment Best Practices

1. **Always provide descriptive filenames**: Use meaningful names like "temperature_report.csv"
2. **Set appropriate MIME types**: This helps tools and the assistant handle attachments correctly
3. **Use functional composition**: Chain tools together rather than creating intermediate
   attachments
4. **Return attachments as the last expression**: The last expression is what gets sent to the
   assistant
5. **Access fields safely**: Use `.get()` when working with tool results

#### Attachment Notes

- Any attachment dict returned from your script (as the final expression) is automatically sent to
  the assistant with the correct metadata
- Nested lists: `[[att1, att2], att3]` - all attachments are extracted automatically
- Tools that accept attachment IDs can receive attachment dicts directly
- Check your configuration for attachment size limits (typically 100MB max)

### JSON Functions

```python
data = {"tasks": ["review PR", "update docs"]}
json_str = json_encode(data)
parsed = json_decode('{"name": "test", "value": 42}')
```

### Base64 Functions

```python
encoded = base64_encode("Hello, World!")
encoded = base64_encode(b"\xff\xfe")
text = base64_decode(encoded)
raw = base64_decode_bytes(encoded)
```

### LLM API Functions

Scripts can make one-shot LLM calls for summarisation, data extraction, classification, and similar
tasks. The default model is `gemini-3-flash-preview`.

#### `llm(prompt, system=None, model=None)`

```python
summary = llm("Summarise this text: " + long_text)
sentiment = llm(
    "What is the sentiment of this review?",
    system="Respond with exactly one word: positive, negative, or neutral.",
)
result = llm("Translate to French: Hello world", model="gpt-4o")
```

#### `llm_json(prompt, schema=None, system=None, model=None)`

```python
data = llm_json("Extract the name and age from: John is 30 years old")

schema = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}
metadata = llm_json("Extract metadata from: " + article_text, schema=schema)
```

### Time API Functions

#### Creating Time Objects

```python
now = time_now()
now_utc = time_now_utc()
meeting_time = time_create(
    year=2024,
    month=3,
    day=15,
    hour=14,
    minute=30,
    second=0,
    timezone_name="America/New_York",
)
date = time_parse("2024-03-15 14:30", "%Y-%m-%d %H:%M", "UTC")
timestamp_time = time_from_timestamp(1710515400, 0)
```

#### Formatting and Timezones

```python
formatted = time_format(now, "%Y-%m-%d %H:%M:%S")
la_time = time_in_location(now, "America/Los_Angeles")
if timezone_is_valid("Europe/London"):
    london_time = time_in_location(now, "Europe/London")
```

#### Time Components

```python
year = time_year(now)
month = time_month(now)
day = time_day(now)
hour = time_hour(now)
minute = time_minute(now)
second = time_second(now)
weekday = time_weekday(now)  # 0=Monday, 6=Sunday
if is_weekend(now):
    print("It's the weekend!")
if is_between(9, 17, now):
    print("Business hours")
```

#### Time Arithmetic

```python
tomorrow = time_add(now, DAY)  # DAY = 86400 seconds
next_hour = time_add(now, HOUR)  # HOUR = 3600 seconds
future = time_add_duration(now, 3, "days")
meeting_end = time_add_duration(meeting_time, 90, "minutes")
diff_seconds = time_diff(future, now)
```

#### Time Comparisons

```python
if time_before(now, meeting_time):
    print("Meeting hasn't started yet")
if time_after(now, deadline):
    print("Deadline has passed")
if time_equal(t1, t2):
    print("Times are identical")
```

#### Duration Handling

```python
duration = duration_parse("2h30m")  # Returns seconds: 9000
human = duration_human(3665)  # Returns: "1h1m5s"
# Constants: SECOND = 1, MINUTE = 60, HOUR = 3600, DAY = 86400, WEEK = 604800
```

### Global Variables

Scripts can receive global variables when executed:

```python
if "user_email" in globals():
    send_email(to=user_email, subject="Reminder", body="...")
```

## Security Model

- **Tool Access**: Scripts only have access to tools allowed by the current profile
- **Sandboxing**: No file system, network, or system access
- **Timeout**: Scripts timeout after 10 minutes (to allow for external API calls)
- **No Imports**: Cannot import external code

## Common Patterns

### Search and Summarize Notes

```python
def summarize_project_notes(project_name):
    notes = search_notes(query=project_name)
    if len(notes) == 0:
        return "No notes found for " + project_name
    summary = "Found " + str(len(notes)) + " notes for " + project_name + ":\n\n"
    for note in notes:
        summary += "- " + note["title"] + "\n"
    add_or_update_note(title=project_name + " Summary", content=summary)
    return summary


summarize_project_notes("Project Alpha")
```

### Process TODOs

```python
def collect_todos():
    notes = search_notes(query="TODO")
    todos = []
    for note in notes:
        if "TODO" in note.get("content", ""):
            todos.append(note["title"])
    if len(todos) > 0:
        content = "# Active TODOs\n\n"
        for todo in todos:
            content += "- [ ] " + todo + "\n"
        add_or_update_note(
            title="TODO List - " + str(len(todos)) + " items", content=content
        )
    return {"count": len(todos), "items": todos}


collect_todos()
```

### Calendar-Based Automation

```python
def create_meeting_prep_notes():
    events = get_calendar_events(days_ahead=1)
    for event in events:
        if "meeting" in event.get("summary", "").lower():
            add_or_update_note(
                title="Prep: " + event["summary"],
                content="Meeting at " + event["start"] + "\n\nAgenda:\n- \n\nNotes:\n",
            )
    return "Created prep notes for " + str(len(events)) + " meetings"


create_meeting_prep_notes()
```

### Email Digest

```python
def create_email_digest(search_term):
    emails = search_emails(query=search_term)
    if len(emails) == 0:
        return "No emails found"
    digest = "# Email Digest: " + search_term + "\n\n"
    for email in emails[:10]:
        digest += "**From**: " + email.get("sender", "Unknown") + "\n"
        digest += "**Subject**: " + email.get("subject", "No subject") + "\n"
        digest += "---\n\n"
    add_or_update_note(title="Email Digest - " + search_term, content=digest)
    return "Created digest with " + str(len(emails)) + " emails"


create_email_digest("project update")
```

### Data Visualization Pipeline

```python
def visualize_data(days=7):
    events = get_calendar_events(days_ahead=days)
    data = []
    for event in events:
        if "temp" in event.get("summary", "").lower():
            data.append({"date": event["start"], "temperature": 72})
    data_json = json_encode(data)
    data_file = attachment_create(
        content=data_json,
        filename="data.json",
        description="Data for past " + str(days) + " days",
        mime_type="application/json",
    )
    chart_spec = json_encode({
        "mark": "line",
        "encoding": {
            "x": {"field": "date", "type": "temporal"},
            "y": {"field": "temperature", "type": "quantitative"},
        },
    })
    chart = create_vega_chart(spec=chart_spec, data_attachments=[data_file])
    return chart


visualize_data(7)
```

## Currently Not Available

- **StateAPI**: No persistent storage between script runs
- **File/Network Access**: Scripts are sandboxed with no filesystem or network access
- **Module Imports**: Cannot import external code
- **Random Numbers**: No random number generation for determinism

## Working with Tool Results

- Tools return structured data (dicts, lists) directly - use results immediately
- `json_decode()` is safe to call on any value - it passes through dicts, lists, numbers, and
  booleans unchanged
- Check for empty results before processing

## Error Handling

```python
notes = search_notes(query="test")
if len(notes) > 0:
    # Process notes...
    pass

if event.get("new_state") and event.get("new_state", {}).get("state"):
    value = int(event.get("new_state", {}).get("state", "0").split(".")[0])
else:
    value = 0
```

## Performance Tips

- Scripts timeout after 10 minutes (but try to keep them efficient)
- Avoid processing very large datasets in memory
- Limit search results when possible
- Use specific queries to reduce result sets
- Be mindful of external API rate limits when making many tool calls

## When to Use Scripts

Use scripts when users request:

- Complex automation with multiple steps
- Data processing and transformation
- Conditional logic based on search results
- Scheduled or event-triggered automation
- Batch operations across multiple items

## Script Development Best Practices

1. **Understand the request**: Clarify what the user wants to achieve
2. **Check available tools**: Use `tools_list()` if unsure what's available
3. **Handle edge cases**: Check for empty results, invalid data
4. **Test first**: For complex scripts, test key operations separately
5. **Provide feedback**: Use print() to show progress for long operations
