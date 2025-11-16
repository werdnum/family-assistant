# Code Hints

This directory contains ast-grep rules that provide helpful suggestions without blocking work.
Unlike conformance rules that cause lint failures, hints are informational and help developers
follow best practices.

## How Hints Work

- **Non-blocking**: Hints never cause lint failures or block commits
- **Smart filtering**: Only shown for newly added/modified code to avoid spam on pre-existing issues
- **Informational**: Display with 💡 icon in PostToolUse hook output
- **Easy to add**: Just create a new YAML rule file in this directory

## When Hints Appear

Hints are shown by the `.claude/lint-hook.py` PostToolUse hook:

- **For Edit operations**: Only hints where the matched code appears in the newly edited content
- **For Write operations**: Only for new files (not tracked by git), to avoid spam on file rewrites

## Active Hints

### `async-magic-mock`

**Pattern**: `MagicMock` or `Mock` used in async test functions

**Why**: `MagicMock` doesn't properly handle async methods. Use `AsyncMock` instead.

**Example**:

```python
# ❌ Will cause issues with async methods
async def test_something():
    mock_service = MagicMock()  # ← Hint appears here

# ✅ Correct approach
async def test_something():
    mock_service = AsyncMock()
```

### `test-mocking-guideline`

**Pattern**: Any Mock/MagicMock/AsyncMock in test files

**Why**: Project guidelines strongly recommend using real or fake objects instead of mocks for
better test reliability.

**Guidance**:

- Prefer real implementations when practical
- Use fake implementations for external services
- Reserve mocks for truly external dependencies (e.g., Telegram API, external HTTP services)

### `toolresult-data-text-warning`

**Pattern**: `ToolResult` with both `text` and `data` parameters

**Why**: When both fields are provided, `text` is sent to LLMs while `data` is used by
scripts/tests. Even with dynamic text (f-strings, expressions), the text might only convey metadata
(e.g., "Found 5 items") rather than the actual data.

**Example**:

```python
# ⚠️ Text has metadata but not actual data
return ToolResult(text=f"Retrieved {len(results)} results", data=results)

# ✅ Text includes actual data or is omitted
return ToolResult(text=f"Results: {results}", data=results)
return ToolResult(data=results)  # Auto-generates text from data
```

**Note**: String literals with data are blocked by the conformance rule
`toolresult-text-literal-with-data`. This hint catches the subtler cases where text is dynamic but
still doesn't convey the data content.

## Adding New Hints

1. **Create hint rule file**: `.ast-grep/rules/hints/<hint-id>.yml`

   ```yaml
   id: my-hint
   language: python
   severity: hint  # Use 'hint' severity
   message: "Brief suggestion message"
   note: |
     Detailed explanation of the suggestion.

     Include examples of better alternatives.
   rule:
     pattern: $PATTERN
   ```

2. **Test the hint**:

   ```bash
   ast-grep scan --rule .ast-grep/rules/hints/my-hint.yml tests/
   ```

3. **Commit**: Hints are automatically picked up by the lint hook

## Hints vs Conformance Rules

| Feature        | Hints                       | Conformance Rules            |
| -------------- | --------------------------- | ---------------------------- |
| **Purpose**    | Suggestions, best practices | Hard requirements            |
| **Blocking**   | Never blocks work           | Blocks commits if violated   |
| **Exemptions** | Not needed                  | Inline/block/file exemptions |
| **Severity**   | `hint`                      | `error`                      |
| **Location**   | `.ast-grep/rules/hints/`    | `.ast-grep/rules/`           |
| **When shown** | Only on new/changed code    | Always (unless exempted)     |

## Implementation Details

Hints are implemented by:

1. **`.ast-grep/check-hints.py`**: Runs ast-grep on hint rules and outputs JSON
2. **`.claude/lint-hook.py`**:
   - Calls check-hints.py after file edits
   - Filters hints to only new/changed code
   - Displays with 💡 icon
   - Never affects lint success/failure

## Philosophy

Hints help developers learn best practices without being intrusive. They appear at the right moment
(when writing code) but don't interrupt flow or require exemptions for legacy code.
