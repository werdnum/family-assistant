# Code Hints

This directory holds the ast-grep rules with `severity: hint`. Hints never fail a build, block a
commit, or need an exemption — they are suggestions surfaced by the `format-and-lint` plugin's
PostToolUse hook after Claude edits a file, via `.ast-grep/check-hints.py` (which always exits 0).
Nothing in pre-commit, `poe lint`, or CI runs them.

For when to add a hint rather than a conformance rule, and for the rule-authoring steps (including
the required test fixtures under `.ast-grep/tests/hints/`), see
[.ast-grep/CLAUDE.md](../../CLAUDE.md). Blocking rules are catalogued in
[../README.md](../README.md).

This file is the catalogue of active hints.

## `async-magic-mock`

**Pattern**: `MagicMock(...)` or `Mock(...)` inside an `async def`. Test files only (the
`test_only_hints` set in `check-hints.py`).

**Why**: `MagicMock` does not handle async methods, so the mock fails once it is `await`ed. Use
`AsyncMock`.

## `test-mocking-guideline`

**Pattern**: any `Mock(...)`, `MagicMock(...)`, or `AsyncMock(...)`. Test files only.

**Why**: project guidelines prefer real or fake implementations over mocks. Use a real object when
it is lightweight and has no external dependencies, a fake for external services, and a mock only
for genuinely external dependencies such as the Telegram API.

## `no-wait-for-selector-then-click`

**Pattern**: `$VAR = await $PAGE.wait_for_selector($$$)`.

**Why**: `wait_for_selector()` returns an `ElementHandle` that goes stale if React re-renders before
you use it, producing "Element is not attached to the DOM". Prefer the Locator API, which retries
automatically:

```python
button = page.locator('[data-testid="submit"]')
await button.click(timeout=10000)
```

If you are only checking element state without interacting, `wait_for_selector()` is fine.

## `toolresult-data-text-warning`

**Pattern**: `ToolResult` with both `text` and `data`, in either order.

**Why**: `text` is sent to the LLM while `data` is used by scripts and tests. Even a dynamic `text`
may convey only metadata (`f"Found {count} items"`) rather than the data itself. Either put the data
in `text` or omit `text` and let it be generated from `data`.

String *literals* for `text` alongside `data` are a hard error instead — see the conformance rule
`toolresult-text-literal-with-data`. This hint catches the subtler dynamic cases.
