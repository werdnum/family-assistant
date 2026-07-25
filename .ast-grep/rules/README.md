# Code Conformance Rules

This directory holds the ast-grep rules with `severity: error` that block commits. This file is the
catalogue — what each active rule bans and what to use instead.

For how the rules are enforced, how to add a new one (including the required test fixtures), and the
pattern-matching gotchas, see [.ast-grep/CLAUDE.md](../CLAUDE.md). For exemptions — the three forms,
when they are justified, and how to audit them — see [EXEMPTIONS.md](../EXEMPTIONS.md). Non-blocking
hints are catalogued in [hints/README.md](hints/README.md).

## Test Anti-Patterns

These four rules only run against files under `tests/` (the `test_only_rules` set in
`check-conformance.py`).

### `no-asyncio-sleep-in-tests`

**Pattern**: `asyncio.sleep($$$)`, except inside a `while` statement — polling intervals in a
condition-checking loop are fine, arbitrary "hope things are done" waits are not.

**Replacement**: `wait_for_condition()` from `tests/helpers.py`.

```python
from tests.helpers import wait_for_condition

await wait_for_condition(lambda: check_some_state(), timeout=5.0, interval=0.1)
```

### `no-time-sleep-in-tests`

**Pattern**: `time.sleep($$$)` or a bare `sleep($$$)`. Unlike the asyncio rule, there is no
while-loop carve-out.

**Why it's banned**: blocking sleep stalls the event loop, making async tests slow and unreliable.

**Replacement**: `wait_for_condition()`, as above.

### `no-playwright-wait-for-timeout`

**Pattern**: `$PAGE.wait_for_timeout($$$)`, except inside a `while` statement.

**Replacement**: Playwright's built-in waits.

```python
await page.wait_for_selector("#element", state="visible")
await page.get_by_text("Submit").wait_for(state="visible")
await expect(page.locator("#status")).to_have_text("Complete")
await page.wait_for_load_state("networkidle")
```

### `no-strict-assistant-message-wait`

**Pattern**: `await ...wait_for(...)` on a locator built from `locator($SEL)` or
`get_by_test_id($TEST_ID)` where the selector matches the regex `assistant-message` — including the
case where the locator was assigned to a variable earlier in the same async function.

**Why it's banned**: chat tests legitimately render multiple assistant messages (an initial response
followed by a final one after tool calls). Playwright's `wait_for()` expects a single matching
element, so the wait fails in strict mode before the test reaches its real assertions.

**Replacement**:

```python
await chat_page.wait_for_message_content("I'll add several notes for you.")
await page.wait_for_selector('[data-testid*="tool-call"]', state="visible")
# Or, if one assistant message is genuinely intended, scope it explicitly:
await page.locator('[data-testid="assistant-message"]').first.wait_for(state="visible")
```

## Datetime Correctness

### `no-naive-datetime-now`

**Pattern**: `datetime.now()` or `datetime.datetime.now()` with no arguments.

**Why it's banned**: a naive datetime silently carries the local process timezone. Comparing it with
a timezone-aware value from the database raises `TypeError` on PostgreSQL and compares wrongly on
SQLite; `.timestamp()` reinterprets it as local time; and values stored in `DateTime(timezone=True)`
columns get mislabelled.

**Replacement**: pass an explicit tzinfo — `datetime.now(UTC)` for absolute instants (storage,
comparisons, cutoffs), or `datetime.now(exec_context.timezone)` for wall-clock time in the user's
configured zone.

### `no-naive-datetime-fromtimestamp`

**Pattern**: `datetime.fromtimestamp($TS)` or `datetime.datetime.fromtimestamp($TS)` with no `tz`.

**Why it's banned**: same hazard as above — the conversion uses the local process timezone, which is
typically UTC in containers but local time on a developer machine, so bugs only surface in CI or
production.

**Replacement**: `datetime.fromtimestamp(ts, tz=UTC)`, or `tz=exec_context.timezone` for a
user-facing time.

## Tool Development Anti-Patterns

### `toolresult-text-literal-with-data`

**Pattern**: `ToolResult` with a string *literal* for `text` alongside a `data` argument (in either
order, with or without other arguments).

**Why it's banned**: `text` is what the LLM sees, `data` is what scripts and tests consume. A string
literal means the LLM receives only a constant message and never sees the actual data.

**Replacement**: use an f-string or expression, or omit `text` and let it be generated from `data`.

```python
return ToolResult(text=f"Found {len(items)} items", data=items)
return ToolResult(text=json.dumps(result), data=result)
return ToolResult(data=result)
```

If the literal genuinely conveys equivalent information (a short error message, say), exempt it:

```python
# ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
return ToolResult(text="Error: Invalid input", data={"error": "Invalid input"})
```

## Note Visibility Confinement

### `no-unconstrained-note-write-policy`

**Pattern**: any mention of `NoteWritePolicy.UNCONSTRAINED`.

**Why it's banned**: `UNCONSTRAINED` disables see-before-overwrite and default/required/allowed
label enforcement for a note write. It is the deliberate opt-out for trusted admin surfaces, so it
must stay greppable and reviewed rather than spreading.

**Replacement**: derive the policy from the active profile with `exec_context.note_write_policy()`,
so label confinement remains a property of the storage layer rather than of one caller.

The approved bypasses (the `NoteWritePolicy` definition itself, the web notes admin API, and the
call-transcript writer) are listed in `.ast-grep/exemptions.yml`; a new admin surface that genuinely
needs the bypass belongs there with a justification.

## Type Annotation Quality

### `no-dict-any`

**Pattern**: `dict[str, Any]` or `Dict[str, Any]` in a variable annotation, a function return
annotation, a nested position inside either (e.g. `list[dict[str, Any]] | None`), or a type alias.

**Why it's flagged**: the annotation effectively disables static typing for the structure and
encourages "JSON blob" dictionaries whose shape only exists in runtime `dict` probing.

**Replacement**: a `TypedDict`, dataclass, or `Protocol` documenting the expected keys and value
types. Keep `dict[str, Any]` only when the data really is arbitrary.
