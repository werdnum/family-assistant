# ast-grep Guide for This Project

For pattern syntax, rule composition (`all`/`any`/`not`/`has`/`inside`), metavariables, and the YAML
rule schema, use the upstream docs — this file only covers how ast-grep is wired into this project.

- [Official documentation](https://ast-grep.github.io/)
- [Pattern syntax guide](https://ast-grep.github.io/guide/pattern-syntax.html)
- [Rule configuration reference](https://ast-grep.github.io/guide/rule-config.html)
- [Interactive playground](https://ast-grep.github.io/playground.html)
- [Project ast-grep recipes](../docs/development/ast-grep-recipes.md) - transformation examples

## How ast-grep is Used in This Project

Project configuration lives in `sgconfig.yml` (repo root), which points `ruleDirs` at
`.ast-grep/rules`.

Rules are enforced at these points:

1. **Post-edit hook** - the `format-and-lint` plugin's PostToolUse hook runs after Claude edits
   files and surfaces both conformance violations and hints. See
   [.claude/README_HOOKS.md](../.claude/README_HOOKS.md).
2. **Pre-commit** - the `code-conformance` hook in `.pre-commit-config.yaml` runs
   `.ast-grep/check-conformance.py` on staged Python files and blocks the commit on violations.
3. **`poe lint`** - `scripts/format-and-lint.sh` runs `check-conformance.py` as part of the lint
   suite, so `poe test` and CI cover it too.
4. **Manual** - `scripts/check-conformance.sh [files...]` (wrapper that cds to repo root and execs
   `check-conformance.py`).

Note that hints are only surfaced by the post-edit hook; nothing in pre-commit, lint, or CI runs
`check-hints.py`.

### Directory Structure

```
.ast-grep/
├── rules/                     # Conformance rules (severity: error)
│   ├── README.md              # Documents every active conformance rule
│   ├── no-asyncio-sleep-in-tests.yml
│   ├── no-dict-any.yml
│   ├── no-naive-datetime-fromtimestamp.yml
│   ├── no-naive-datetime-now.yml
│   ├── no-playwright-wait-for-timeout.yml
│   ├── no-strict-assistant-message-wait.yml
│   ├── no-time-sleep-in-tests.yml
│   ├── no-unconstrained-note-write-policy.yml
│   ├── toolresult-text-literal-with-data.yml
│   └── hints/                 # Hint rules (severity: hint)
│       ├── README.md
│       ├── async-magic-mock.yml
│       ├── no-wait-for-selector-then-click.yml
│       ├── test-mocking-guideline.yml
│       └── toolresult-data-text-warning.yml
├── tests/
│   ├── conformance/           # <rule-id>-positive.py / <rule-id>-negative.py
│   └── hints/
├── check-conformance.py       # Conformance checker with exemption handling
├── check-hints.py             # Hints checker (always exits 0)
├── test-rules.py              # Rule test runner
├── add-exemptions.py          # Bulk-adds inline ast-grep-ignore comments
├── exemptions.yml             # File-level exemptions
└── EXEMPTIONS.md              # Exemptions guide
```

## Conformance Rules vs Hints

**Conformance rules** live in `.ast-grep/rules/`, use `severity: error`, block commits, and can be
exempted. Add one when the pattern reliably indicates a problem and a concrete alternative exists.

**Hints** live in `.ast-grep/rules/hints/`, use `severity: hint`, and never fail a build. Add one
when the pattern only suggests an improvement or has legitimate exceptions.

## Adding a New Rule

1. Create `.ast-grep/rules/<rule-id>.yml` (or `rules/hints/<rule-id>.yml`) with a kebab-case `id`,
   `language`, `severity`, a one-line `message`, and a `note` explaining the problem, the
   replacement, and any expected exemptions. Rule IDs follow `no-banned-pattern` /
   `require-good-pattern` for conformance and `prefer-*` / `consider-*` for hints.

2. Add test fixtures under `.ast-grep/tests/conformance/` (or `tests/hints/`) named
   `<rule-id>-positive.py` (must match) and `<rule-id>-negative.py` (must not match), then run:

   ```bash
   .ast-grep/test-rules.py --rule <rule-id>   # or with no args to test all rules
   ```

   Fixture files intentionally contain violations, so add a file-level entry in `exemptions.yml` for
   them.

3. If the rule should only apply to test files, add its ID to the `test_only_rules` set in
   `check-conformance.py` (or `test_only_hints` in `check-hints.py`). Otherwise it runs against all
   Python files.

4. Verify against the real codebase and handle pre-existing violations:

   ```bash
   ast-grep scan --rule .ast-grep/rules/<rule-id>.yml src/
   .ast-grep/check-conformance.py src/
   .ast-grep/add-exemptions.py <rule-id> --dry-run   # bulk inline exemptions; review first
   ```

5. Document the rule in `.ast-grep/rules/README.md` (or `rules/hints/README.md`).

## Exemptions

Three forms, in order of preference:

```python
# ast-grep-ignore: rule-id - Reason
problematic_code()

# ast-grep-ignore-block: rule-id - Reason
first_violation()
second_violation()
# ast-grep-ignore-end
```

```yaml
# .ast-grep/exemptions.yml
exemptions:
  - rule: rule-id
    files:
      - path/to/file.py
      - path/to/*.py
    reason: "Why the exemption is needed"
    ticket: "#123"  # Optional
```

Every exemption needs a reason. Use them when the spirit of the rule genuinely doesn't apply or
there is no practical alternative — not to defer a fix that should happen. Run
`scripts/audit-conformance-exemptions.sh` to review active exemptions. See
[EXEMPTIONS.md](EXEMPTIONS.md) for the full guide.

## Gotchas

- Unnamed AST nodes (such as Python's `async` keyword) cannot be matched directly. Use
  `pattern: {context: 'async def $FUNC($$$ARGS): $$$BODY', selector: function_definition}`, or match
  on `kind` with additional constraints.
- Matching a keyword argument in any position needs several patterns under `any` (leading, trailing,
  and sole-argument variants), because `$$$` cannot straddle the argument you are pinning. See
  [docs/development/ast-grep-recipes.md](../docs/development/ast-grep-recipes.md).
- ast-grep matches AST nodes, so it never sees comments or string contents. Use grep for those.

## Related Documentation

- [Code conformance rules](rules/README.md) - active rules and their replacements
- [Hint rules](rules/hints/README.md)
- [Exemptions guide](EXEMPTIONS.md)
- [ast-grep recipes](../docs/development/ast-grep-recipes.md)
- [Testing guide](../tests/CLAUDE.md)
