# Code Conformance Exemptions Guide

How to exempt code from the conformance rules catalogued in [rules/README.md](rules/README.md).

## When to Use Exemptions

Exempt code when the spirit of the rule genuinely doesn't apply in this context, or when there is no
practical alternative to the pattern. Do not exempt code to defer a fix that should happen, to
bypass review, or to hide technical debt with no plan to address it.

## Exemption Types

### 1. Inline Exemptions (Preferred for Small Cases)

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - Simulating network delay for timeout testing
await asyncio.sleep(30)
```

**Format**: `# ast-grep-ignore: <rule-id> - <reason>`. The reason is part of the parsed syntax, not
a convention — a comment without ` - <reason>` does not register as an exemption at all.

**Scope**: the immediately following line. A blank line or a comment between the marker and the
violation breaks it.

### 2. Block Exemptions (For Multiple Lines)

```python
# ast-grep-ignore-block: no-asyncio-sleep-in-tests - Mock implementation simulates delays
async def mock_slow_network_call():
    await asyncio.sleep(0.5)
    return {"status": "ok"}
# ast-grep-ignore-end
```

**Format**: opens with `# ast-grep-ignore-block: <rule-id> - <reason>`, closes with
`# ast-grep-ignore-end`.

**Scope**: every non-comment line between the markers. Best for several related violations in one
function, mock implementations, and test helpers with real timing needs.

### 3. File-Level Exemptions (For Systematic Cases)

Add an entry to `.ast-grep/exemptions.yml`:

```yaml
exemptions:
  - rule: no-asyncio-sleep-in-tests
    files:
      - tests/helpers.py
      - tests/mocks/*.py
    reason: |
      Test infrastructure files that implement timing-sensitive helpers
      and mock objects with deliberate delays.
    ticket: null
```

Best for infrastructure files (helpers, mocks, fixtures), whole modules with a consistent need,
generated code, and gradual migration of legacy code. Test fixtures under `.ast-grep/tests/`
deliberately contain violations and are exempted here too.

## Exemption Requirements

Every exemption needs the specific rule ID and a reason explaining *why* it is needed; add a ticket
number when the exemption is meant to be removed later. A reason must say what makes this case
different — "needed", "too hard to fix right now", and "this is test infrastructure" do not.
Compare:

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - wait_for_condition() implementation itself
# ast-grep-ignore: no-playwright-wait-for-timeout - CSS transition is a fixed 300ms, no selector to wait for
```

For a legacy backlog, put the plan in the reason and track it:

```yaml
reason: |
  Legacy test code that needs refactoring to use condition-based waits.
ticket: "#456"
```

## Managing Exemptions

Audit the active exemptions with:

```bash
scripts/audit-conformance-exemptions.sh
```

It prints every entry in `.ast-grep/exemptions.yml` with its file count, first line of reason, and
ticket, followed by the total number of violations currently exempted.

To remove an exemption, delete the comment or the `exemptions.yml` entry, then run
`scripts/check-conformance.sh` to confirm nothing is left unhandled.

For a large migration, start with a file-level exemption covering the existing violations and a
ticket, which stops *new* violations immediately, then narrow the glob as you fix files until the
entry can be deleted.

If you find yourself adding many exemptions for one rule, that is a signal to question the rule or
restructure the code rather than to keep exempting.

## Troubleshooting

If an exemption isn't taking effect, check the comment format exactly, check that an inline marker
sits immediately before the violation, check that the rule ID matches case-sensitively, and check
that any glob in `.ast-grep/exemptions.yml` matches the file path as reported by the checker.

## Resources

- [Code conformance rules](rules/README.md)
- [ast-grep guide for this project](CLAUDE.md)
- [Project testing guide](../tests/CLAUDE.md)
