# Code Conformance Rules

This directory contains ast-grep rules that enforce code quality standards and prevent anti-patterns
in the codebase.

## Overview

Code conformance rules are enforced at multiple points:

- **Pre-commit hooks**: Blocks commits with new violations
- **`poe lint`**: Includes conformance checking in the full lint suite
- **Claude lint hook**: Shows violations after file edits
- **Manual checks**: `scripts/check-conformance.sh [files...]`

## Active Rules

### Test Anti-Patterns

#### `no-asyncio-sleep-in-tests`

**Pattern**: `asyncio.sleep($$$)` in test files

**Why it's banned**: Using `asyncio.sleep()` in tests leads to flaky tests and slow test suites.
Fixed delays are always either too short (causing failures under load) or too long (wasting time).

**Replacement**:

```python
from tests.helpers import wait_for_condition

# Instead of: await asyncio.sleep(2)
# Use:
await wait_for_condition(
    lambda: check_some_state(),
    timeout_seconds=5.0,
    poll_interval_seconds=0.1
)
```

#### `no-time-sleep-in-tests`

**Pattern**: `time.sleep($$$)` or `sleep($$$)` in test files

**Why it's banned**: Blocking sleep calls block the event loop and make async tests unreliable.

**Replacement**: Use `wait_for_condition()` helper (same as above)

#### `no-playwright-wait-for-timeout`

**Pattern**: `$PAGE.wait_for_timeout($$$)` in test files

**Why it's banned**: `wait_for_timeout()` is fragile and makes tests slower. Playwright provides
better alternatives.

**Replacement**:

```python
# Wait for element to be visible
await page.wait_for_selector("#element", state="visible")

# Wait for element with text
await page.get_by_text("Submit").wait_for(state="visible")

# Wait for condition using expect
from playwright.async_api import expect
await expect(page.locator("#status")).to_have_text("Complete")

# Wait for network to be idle
await page.wait_for_load_state("networkidle")
```

### Tool Development Anti-Patterns

#### `toolresult-text-literal-with-data`

**Pattern**: `ToolResult` with a string literal for `text` parameter and `data` parameter

**Why it's banned**: When `ToolResult` has both `text` (as a string literal) and `data`, the `text`
field is sent to LLMs while the `data` field is used by scripts and tests. Using a string literal
for `text` means the LLM receives only a constant message and has no access to the actual data in
the `data` field.

**Replacement**:

```python
# ❌ INCORRECT - LLM receives useless constant
return ToolResult(text="Here is the data", data=actual_data)
return ToolResult(text="Operation completed", data=result)

# ✅ CORRECT - Use f-string, expression, or omit text
return ToolResult(text=f"Found {len(items)} items", data=items)
return ToolResult(text=json.dumps(result), data=result)
return ToolResult(data=result)  # text auto-generated from data
```

**Note**: If the string literal genuinely conveys equivalent information (rare), you can add an
exemption:

```python
# ast-grep-ignore: toolresult-text-literal-with-data - Error message is sufficient
return ToolResult(text="Error: Invalid input", data={"error": "Invalid input"})
```

### Type Annotation Quality

#### `no-dict-any`

**Pattern**: Type annotations that include `dict[str, Any]` (or `Dict[str, Any]`)

**Why it's flagged**: These annotations effectively disable static typing for the structure. They
encourage "JSON blob" dictionaries whose shape is unclear and makes downstream logic rely on runtime
`dict` probing.

**Replacement**: Define a structured type (e.g., `TypedDict`, dataclass, or `Protocol`) that
documents the expected keys and value types. Only use `dict[str, Any]` if the data is legitimately
arbitrary.

## Adding Exemptions

Sometimes you legitimately need to use a banned pattern. There are three ways to add exemptions:

### 1. Inline Exemption (Single Line)

Add a comment on the line immediately before the violation:

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - Simulating hardware timeout
await asyncio.sleep(5)
```

### 2. Block Exemption (Multiple Lines)

Wrap the code block with exemption markers:

```python
# ast-grep-ignore-block: no-asyncio-sleep-in-tests - Legacy code needs refactoring (#123)
await asyncio.sleep(1)
do_something()
await asyncio.sleep(2)
# ast-grep-ignore-end
```

### 3. File-Level Exemption

Add to `.ast-grep/exemptions.yml`:

```yaml
exemptions:
  - rule: no-asyncio-sleep-in-tests
    files:
      - tests/helpers.py # Helper implements wait utilities
      - tests/mocks/*.py # Mock implementations
    reason: "Test infrastructure legitimately needs sleep"
    ticket: "#456" # Optional
```

**Exemption Requirements**:

- Must include the rule ID being exempted
- Must include a clear reason
- Should reference a ticket number for tracking future removal (when applicable)

## Adding New Rules

To add a new conformance rule:

1. **Create rule file**: `.ast-grep/rules/<rule-id>.yml`

   ```yaml
   id: my-new-rule
   language: python
   severity: error
   message: "Short description of what's banned"
   note: |
     Detailed explanation of why this pattern is banned.

     Suggested replacement with examples.
   rule:
     pattern: $PATTERN
   ```

2. **Test the rule**:

   ```bash
   # Test on specific files
   ast-grep scan tests/some_file.py

   # Test on all files
   ast-grep scan tests/
   ```

3. **Add exemptions** for existing violations (if needed)

4. **Update test-only rules list** in `.ast-grep/check-conformance.py` if rule only applies to tests

5. **Document the rule** in this README

6. **Commit and deploy**

## Auditing Exemptions

To review all active exemptions:

```bash
scripts/audit-conformance-exemptions.sh
```

This shows:

- All exemption entries from `.ast-grep/exemptions.yml`
- Number of files covered by each exemption
- Total number of violations currently exempted

Periodically review exemptions to identify opportunities for cleanup and technical debt reduction.

## Troubleshooting

### False Positives

If a rule triggers incorrectly, add an exemption with a clear explanation:

```python
# ast-grep-ignore: rule-id - This is actually correct because [reason]
```

### Rule Not Triggering

1. Check rule syntax: `ast-grep scan --rule .ast-grep/rules/your-rule.yml tests/`
2. Verify pattern matches: `ast-grep -p 'your pattern' tests/`
3. Ensure file paths are correct (test-only rules need path filtering)

### Performance Issues

- Rules are cached by ast-grep and run very fast
- The conformance check adds ~0.1-0.3s to lint time
- For large files, increase timeout in `.claude/lint-hook.py`

## Hints (Non-Enforced Guidance)

The `.ast-grep/rules/hints/` directory contains patterns that provide helpful guidance but don't
block commits. These are informational only - use your judgment about whether to apply them.

### `no-wait-for-selector-then-click`

**Pattern**: `$VAR = await $PAGE.wait_for_selector($$$)`

**Guidance**: Consider using Playwright's Locator API instead of `wait_for_selector()` when you plan
to interact with the element, as Locators automatically retry on stale element references.

```python
# ❌ Prone to stale element errors
button = await page.wait_for_selector('[data-testid="submit"]')
await button.click()  # Can fail if React re-renders

# ✅ Robust (auto-retries)
button = page.locator('[data-testid="submit"]')
await button.click(timeout=10000)
```

If you're only checking element state without interacting, `wait_for_selector()` is fine.

## Resources

- [ast-grep Documentation](https://ast-grep.github.io/)
- [ast-grep Pattern Syntax](https://ast-grep.github.io/guide/pattern-syntax.html)
- [Rule Configuration Reference](https://ast-grep.github.io/guide/rule-config.html)
- [Project ast-grep recipes](../../docs/development/ast-grep-recipes.md)
