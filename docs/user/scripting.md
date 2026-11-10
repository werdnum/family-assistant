# Family Assistant Scripting Guide

Reference for writing Python scripts with `execute_script` and automation scripts.

## Editing stored scripts

In the web interface, open **Automation → Scripts**. Choose **New script** or **Edit script** to set
the description, Python code, or optional JSON Schema parameter definition (existing script names
stay fixed so automations keep working). **Save script** validates the schema and syntax, stamps the
complete definition as your own edit, and updates all referencing automations without running the
script. To approve an existing script without editing it, choose **Review and confirm** on the
Scripts page (see
[Reviewing saved artifacts](confirmations-and-safety.md#reviewing-saved-artifacts)).

## Sandbox and Language Rules

Scripts run in the Monty Python sandbox:

- **Allowed imports**: `re`, `math`, `json`, `datetime`, `collections`, `itertools`, `functools`,
  `base64`, `random`, `dataclasses`, `typing` (any other import raises `ModuleNotFoundError`).
- **No filesystem, network, or persistent state**: Use tools, notes, attachments, and
  `keychute_http_request()` instead.
- **Limits**: 256 MB memory, recursion depth 100, 10-minute timeout.
- **Local time**: `datetime.now()` returns the household's local time.
- **Monty syntax restrictions**:
  - No generator functions (`yield`); generator expressions like `sum(x for x in items)` work.
  - No `match`/`case` statements (use `if`/`elif`).
  - No `del` statement (reassign or use `.pop()`).
  - Exceptions accept 0 or 1 string argument (`raise ValueError("msg")`, not
    `raise ValueError("msg", data)`).

## Script Output and Return Value

A script returns its **last expression** (`Script result: ...`). Anything passed to `print()` is
captured (up to 16 KiB in the tool result, truncated with `... [output truncated] ...`; full output
is logged on the server) and returned above the result under `--- Script Output ---`. If the script
raises an error, output printed before the failure is still returned above the error message.

## Tools API and Globals

Tools permitted by the active profile can be called directly as functions or via reflection helpers:

```python
# Direct calls (recommended)
add_or_update_note(title="Meeting Notes", content="...")
events = get_calendar_events(days_ahead=7)

# Dynamic inspection and execution
tools = tools_list()  # [{"name": ..., "description": ...}, ...]
tool_info = tools_get("send_email")  # {"name": ..., "parameters": {...}}
tools_execute("add_or_update_note", title="Shopping List", content="Milk, Eggs")
tools_execute_json("send_email", '{"to": "user@example.com", "subject": "Test"}')
```

Tools return structured Python data (`dict`, `list`, etc.) directly. Check optional globals injected
via `globals` or event triggers with `"var_name" in globals()`.

## Automatic review of a script

When a script needs approval, one approval can cover the whole script, including ordinary
intermediate reads and saves. A script that can send data outside the household, run code in a
sandbox, or save something that runs or loads later is reviewed before it starts — even before
reading external content — because it may read such content while running. Approval binds the exact
source, inputs, and (recursively) any saved child scripts called by literal name. If a saved script
changes while approval is pending, the approved run uses the earlier source; if a child script
changes or disappears before running, prepare and approve the script again.

A review can also approve a script while watching specific step types per call (for example,
checking each forwarded email with its actual recipient and body). Declined watched steps are
skipped while the rest of the script continues; steps too large to check automatically ask you to
confirm (or are declined when nobody can be asked).

Scheduled or event-triggered scripts created from your own request, or approved when saved, run
without re-review. Unapproved scripts saved from external content are reviewed once when they fire;
if not approved, they do not run because nobody is present to confirm. Approval never grants extra
tool permissions. It covers what the script does with what it reads (including `llm()` answers and
commands built for a code sandbox); saving/running code outside the reviewed set, choosing a child
script dynamically, creating future automations, delegating to another assistant, and judgment-based
tools each follow their own approval rules.

## Built-in Script APIs

### Brokered HTTP with Keychute

When [Keychute](https://github.com/werdnum/keychute) is configured, `keychute_http_request()` makes
authenticated HTTPS calls without exposing the credential to Family Assistant:

```python
response = keychute_http_request(
    secret_name="weather-api-token",
    url="https://api.example.com/v1/forecast?city=Melbourne",
    method="GET",
    reason="Fetch the forecast requested by the user",
)
if response["status_code"] == 200:
    forecast = json_decode(response["body"])
```

- **Response**: `{"status_code": int, "headers": dict[str, list[str]], "body": bytes}`
  (`json_decode()` accepts `bytes` directly).
- **Optional arguments**: `headers`, `body` (`str` or `bytes`; for JSON pass
  `body=json_encode(data)` with `Content-Type: application/json`), `ttl_seconds`, `max_uses`,
  `approval_timeout_seconds`, `request_timeout_seconds`.
- **Secret names**: If the secret name is unknown, pick a plausible descriptive name
  (`weather-api-token`, `github-token`, `octoprint`) and make the call rather than asking first; the
  approval UI lets the user redirect to an existing secret or enter a new one.
- **Security and testing**: Keychute attaches the credential server-side, does not follow redirects,
  and binds approval to the script source, destination, method, and constraints. Upstream responses
  are untrusted. `test_script_with_simulated_tools` does not expose brokered HTTP; use it for
  surrounding logic, then `execute_script` for the real Keychute call.

### Attachment API

Attachments are represented as metadata dicts with keys `id`, `filename`, `mime_type`, `size`, and
`description`. Tools taking attachment IDs accept either the ID string or the metadata dict.

- **Reading**:
  - `attachment_read(attachment_id)` -> UTF-8 `str`
  - `attachment_read_bytes(attachment_id)` -> `bytes`
  - `attachment_get(attachment_id)` -> metadata `dict`, or `None` if not found
  - `attachment_read` and `attachment_read_bytes` raise if missing/inaccessible. Copy binary
    attachments to a workspace without entering conversation context via:
    `write_file(cwd=workspace, path="model.glb", content=base64_encode(attachment_read_bytes(attachment_id)))`
- **Creating**:
  - `attachment_create(content, filename, description=None, mime_type="application/octet-stream")`
    -> metadata `dict` (`content` may be `str` or `bytes`).
- **Tool results and propagation**:
  - Single-attachment tools return a metadata `dict`; text + attachment tools return
    `{"text": "...", "attachments": [{...}, ...]}`.
  - Returning an attachment dict (or a list/nested list like `[chart1, chart2]`) as the script's
    last expression automatically attaches those files to the reply. See
    [data_visualization.md](data_visualization.md) for `jq_query` and chart pipelines.

### JSON and Base64 Functions

- `json_encode(obj)` -> JSON `str`
- `json_decode(val)` -> parsed value (accepts `str` or `bytes`; passes already-decoded dicts, lists,
  numbers, and booleans through unchanged)
- `base64_encode(str_or_bytes)` -> base64 `str`
- `base64_decode(encoded)` -> UTF-8 `str`
- `base64_decode_bytes(encoded)` -> raw `bytes`

### LLM and Classifier Functions

- **`llm(prompt, system=None, model=None)`** -> `str` response (default model: `gemini-3.8-flash`).
- **`llm_json(prompt, schema=None, system=None, model=None)`** -> parsed JSON, optionally guided by
  a JSON Schema `dict` (`schema={"type": "object", "properties": {...}}`).
- **`classify(state, question, options)`** -> `{"label": str, "probabilities": dict[str, float]}`
- **`classify_yes_no(state, question, yes=None, no=None)`** -> `float` probability (`0.0` to `1.0`)
  that the answer is yes.

`classify` and `classify_yes_no` use the deterministic TypeSafe classifier (if unconfigured, they
raise an error and `llm_json()` can be used instead). `state` is a text `str` or a `dict` of named
text fields (`{"subject": ..., "body": ...}`) referenced in `question` (no images). `options` is a
list of at least two labels, or a `dict` mapping each label to a description or `None`:

```python
res = classify(email_body, "What is this email about?", {"bills": "Invoices", "other": None})
if res["label"] == "bills" and res["probabilities"]["bills"] > 0.8:
    wake_llm({"message": "A bill arrived", "email": email_body})
```

### Time and Duration API

- **Creation and parsing**: `time_now()` (household local tz), `time_now_utc()`,
  `time_create(year, month, day, hour=0, minute=0, second=0, timezone_name="UTC")`,
  `time_parse(time_str, fmt, timezone_name="UTC")`, `time_from_timestamp(seconds, nanoseconds=0)`
- **Formatting and timezones**: `time_format(t, fmt)` (`strftime`), `time_in_location(t, tz_name)`,
  `timezone_is_valid(tz_name)` -> `bool`
- **Components and checks**: `time_year(t)`, `time_month(t)`, `time_day(t)`, `time_hour(t)`,
  `time_minute(t)`, `time_second(t)`, `time_weekday(t)` (`0`=Mon .. `6`=Sun), `is_weekend(t)`,
  `is_between(start_hour, end_hour, t)`
- **Arithmetic, comparisons, and durations**:
  - `time_add(t, seconds)`, `time_add_duration(t, amount, unit)` (`"seconds"`, `"minutes"`,
    `"hours"`, `"days"`, `"weeks"`), `time_diff(t1, t2)` (`t1 - t2` in seconds)
  - `time_before(t1, t2)`, `time_after(t1, t2)`, `time_equal(t1, t2)` -> `bool`
  - `duration_parse("2h30m")` -> `9000`; `duration_human(3665)` -> `"1h1m5s"`
  - Constants (seconds): `SECOND = 1`, `MINUTE = 60`, `HOUR = 3600`, `DAY = 86400`, `WEEK = 604800`

## Event-Triggered Scripts and `wake_llm`

Event-triggered scripts receive globals `event` (`dict`), `conversation_id`, and `automation_id`.
Condition scripts receive `event` and must return a `bool` (screen out Home Assistant
`"unavailable"`, `"unknown"`, and `None` states before calling `float(...)`). See
[automations.md](automations.md).

- **`wake_llm(context, include_event=True, new_conversation=False)`**: Queues a wake context (`str`
  wrapped as `{"message": context}`, or `dict`) for the LLM after the script finishes (multiple
  calls are batched). Setting `new_conversation=True` (web only) opens a fresh thread off your Home
  (or a new web conversation when threads are off) with no prior history; put all needed context in
  `context` (its first line names the thread). Multiple `new_conversation=True` calls in one run
  share that new thread.
- **Failure notifications**: When an automation script fails after exhausting retries, the assistant
  is woken with the script code, error, and event unless `notify_on_failure: false` is set in
  `action_config`.
