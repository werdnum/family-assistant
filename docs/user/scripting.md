# Family Assistant Scripting Guide

This guide covers Family Assistant-specific APIs and patterns for writing Starlark scripts.
For general Starlark syntax, see the [official Starlark documentation](https://github.com/bazelbuild/starlark/blob/master/spec.md).

## Available APIs

### Tools API

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

### JSON Functions

```starlark

# Encode Python objects to JSON
data = {"tasks": ["review PR", "update docs"]}
json_str = json_encode(data)

# Decode JSON strings
parsed = json_decode('{"name": "test", "value": 42}')

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

## Important Notes

### Currently Not Available

- **TimeAPI**: No `now()`, `today()`, or time comparison functions yet
- **StateAPI**: No persistent storage between script runs
- **Event Triggers**: Scripts must be manually executed

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

```

### Performance Tips

- Scripts timeout after 10 minutes (but try to keep them efficient)
- Avoid processing very large datasets in memory
- Limit search results when possible
- Use specific queries to reduce result sets
- Be mindful of external API rate limits when making many tool calls

## Examples of Script Requests

When asking the assistant to execute scripts, you can say:

- "Execute a script that finds all TODO notes and creates a summary"
- "Run a script to create prep notes for tomorrow's meetings"
- "Write and execute a script that searches for project updates and emails me a digest"
- "Create a script that checks for birthday events and creates reminder notes"
