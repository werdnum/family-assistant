# Code Conformance Exemptions Guide

This guide explains how to use exemptions from code conformance rules effectively.

## When to Use Exemptions

Exemptions should be used when:

1. **The spirit of the rule doesn't apply** - The rule was designed to prevent a specific problem,
   but that problem doesn't exist in this context
2. **The pattern is unavoidable** - There's no practical alternative that doesn't violate the rule

**Do NOT use exemptions** to:

- Avoid fixing code that should be fixed (e.g., "it would take too much time")
- Bypass code review
- Hide technical debt without a plan to address it

## Exemption Types

### 1. Inline Exemptions (Preferred for Small Cases)

Use for a single line that needs an exemption:

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - Simulating network delay for timeout testing
await asyncio.sleep(30)
```

**Format**: `# ast-grep-ignore: <rule-id> - <reason>`

**Scope**: Next non-comment line only

**Best for**:

- One-off legitimate uses
- Cases that need inline documentation
- Specific edge cases

### 2. Block Exemptions (For Multiple Lines)

Use for a code block that needs exemption:

```python
# ast-grep-ignore-block: no-asyncio-sleep-in-tests - Mock implementation simulates delays
async def mock_slow_network_call():
    await asyncio.sleep(0.5)  # Simulate network latency
    return {"status": "ok"}
    await asyncio.sleep(0.1)  # Simulate processing time
# ast-grep-ignore-end
```

**Format**:

- Start: `# ast-grep-ignore-block: <rule-id> - <reason>`
- End: `# ast-grep-ignore-end`

**Scope**: All code between markers (comments excluded)

**Best for**:

- Multiple related violations in one function/block
- Mock implementations
- Test helpers with specific timing needs

### 3. File-Level Exemptions (For Systematic Cases)

Use for entire files or glob patterns:

Edit `.ast-grep/exemptions.yml`:

```yaml
exemptions:
  - rule: no-asyncio-sleep-in-tests
    files:
      - tests/helpers.py
      - tests/mocks/*.py
      - tests/integration/llm/*.py
    reason: |
      Test infrastructure files that implement timing-sensitive helpers
      and mock objects with deliberate delays.
    ticket: null
```

**Best for**:

- Infrastructure files (helpers, mocks, fixtures)
- Entire modules with consistent needs
- Gradual migration of legacy code
- Third-party or generated code

## Exemption Requirements

All exemptions MUST include:

1. **Rule ID**: The specific rule being exempted (e.g., `no-asyncio-sleep-in-tests`)
2. **Clear reason**: Explain WHY the exemption is needed
3. **Optional ticket**: Reference a ticket number for tracking future removal

### Good Exemption Reasons

✅ **Good** (spirit of rule doesn't apply):

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - wait_for_condition() implementation itself
await asyncio.sleep(poll_interval)
```

✅ **Good** (pattern is unavoidable):

```python
# ast-grep-ignore: no-playwright-wait-for-timeout - CSS transition has fixed 300ms duration, no selector to wait for
await page.wait_for_timeout(350)
```

✅ **Good** (legacy code with plan):

```yaml
reason: |
  Legacy test code that needs refactoring to use condition-based waits.
  Scheduled for cleanup - see ticket for details.
ticket: "#456"
```

### Bad Exemption Reasons

❌ **Bad** (too vague):

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - needed
```

❌ **Bad** (no reason):

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests
```

❌ **Bad** (avoids fixing):

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - too hard to fix right now
```

❌ **Bad** (assumes "infrastructure" is always exempt):

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - this is test infrastructure
```

## Managing Exemptions

### Review Exemptions Regularly

Audit current exemptions:

```bash
scripts/audit-conformance-exemptions.sh
```

This shows:

- All file-level exemptions from `.ast-grep/exemptions.yml`
- Number of files and violations covered
- Ticket numbers for tracking

### Removing Exemptions

When you fix violations:

1. **Inline/block exemptions**: Just delete the comment
2. **File-level exemptions**: Remove from `.ast-grep/exemptions.yml`
3. **Verify**: Run `scripts/check-conformance.sh` to ensure no new violations

### Gradual Migration Strategy

For large-scale refactoring:

1. **Initial state**: Add file-level exemptions for all existing violations
2. **Add tracking**: Include ticket numbers in exemptions
3. **Enable enforcement**: Prevents NEW violations
4. **Incremental fixes**: Remove exemptions as you fix files
5. **Final cleanup**: Remove all exemptions when complete

Example workflow:

```yaml
# Initial exemptions.yml
exemptions:
  - rule: no-asyncio-sleep-in-tests
    files:
      - tests/functional/*.py # 45 files with violations
    reason: "Legacy tests need refactoring to use wait_for_condition()"
    ticket: "#789"
```

As you fix files, narrow the exemption:

```yaml
# After fixing some files
exemptions:
  - rule: no-asyncio-sleep-in-tests
    files:
      - tests/functional/test_legacy_*.py # 12 files remaining
    reason: "Legacy tests need refactoring to use wait_for_condition()"
    ticket: "#789"
```

## Examples by Use Case

### Test Infrastructure

```python
# In tests/helpers.py
# ast-grep-ignore-block: no-asyncio-sleep-in-tests - Helper implements polling with sleep
async def wait_for_condition(
    condition: Callable[[], T | Awaitable[T]],
    timeout: float = 30.0,
    interval: float = 0.1,
    description: str = "condition",
) -> T:
    """Poll until condition is true or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        # ... implementation ...
        await asyncio.sleep(interval)  # Legitimate use
    raise TimeoutError(f"Timed out waiting for {description}")
# ast-grep-ignore-end
```

### Mock Objects

```python
# In tests/mocks/mock_api.py
# ast-grep-ignore-block: no-asyncio-sleep-in-tests - Mock simulates realistic API delays
class MockAPIClient:
    async def fetch_data(self):
        await asyncio.sleep(0.05)  # Simulate network latency
        return {"data": "mock"}
# ast-grep-ignore-end
```

### Animation Timing

```python
# In tests/functional/web/test_animations.py
async def test_sidebar_animation():
    await page.click("#toggle-sidebar")
    # ast-grep-ignore: no-playwright-wait-for-timeout - CSS transition duration is 300ms
    await page.wait_for_timeout(350)
    await expect(page.locator("#sidebar")).to_be_visible()
```

### Legacy Code Migration

```yaml
# .ast-grep/exemptions.yml
exemptions:
  - rule: no-asyncio-sleep-in-tests
    files:
      - tests/functional/test_old_*.py
    reason: |
      Legacy functional tests written before wait_for_condition() helper existed.
      Scheduled for refactoring in Q2 2024.
    ticket: "#1234"
```

## Troubleshooting

### Exemption Not Working

1. **Check syntax**: Ensure comment format is exact
2. **Check placement**: Inline exemptions must be immediately before the violation
3. **Check rule ID**: Must match exactly (case-sensitive)
4. **Check file patterns**: Glob patterns in `.ast-grep/exemptions.yml` must match file paths

### Too Many Exemptions

If you find yourself adding many exemptions:

1. **Question the rule**: Is the rule too strict?
2. **Consider refactoring**: Can the code be restructured to avoid the pattern?
3. **Use file-level exemptions**: More maintainable than many inline exemptions
4. **Create a plan**: Add ticket numbers and schedule cleanup

## Best Practices

1. **Be specific**: Explain exactly why the exemption is needed
2. **Add context**: Reference tickets, documentation, or specific requirements
3. **Prefer narrow scope**: Use inline/block over file-level when possible
4. **Review regularly**: Audit exemptions periodically
5. **Plan removal**: Include tickets for tracking future cleanup
6. **Document edge cases**: If it's truly an edge case, explain it well

## Resources

- [Code conformance rules documentation](.ast-grep/rules/README.md)
- [ast-grep documentation](https://ast-grep.github.io/)
- [Project testing guide](../tests/CLAUDE.md)
