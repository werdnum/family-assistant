# Code Review Guidelines

This document defines the severity levels and review criteria used by the automated code review
system. When reviewing code changes, issues are categorized by their potential impact.

## Review Comment Policy

Every comment you post must be **actionable**. A comment is actionable only if it identifies a
concrete problem in the changed code and tells the author what to do differently. The following are
**forbidden** and should never be posted — if you catch yourself writing one, drop it:

- Praise, approval, or "looks good" / "nice work" / "LGTM" style remarks.
- Inline comments that only restate or paraphrase what a specific line, expression, or block does.
  (A reviewer-written restatement of the change's overall intent and strategy belongs in the review
  summary — see "Change Summary in the Review Summary" below — but inline paraphrase is noise.)
- "Please confirm", "please verify", "make sure", "consider whether" style prompts that push the
  work back onto the author without identifying a specific defect.
- Comments hedged with "might", "could potentially", "in some cases" when you have no concrete
  failure mode in mind.
- Questions unless you already know the answer and are using the question form rhetorically to point
  out a defect.
- Speculation about whether a change "might fail tests", "could break the linter", or "may not pass
  type checking" — see "Tests and Linters Are Authoritative" below.
- Purely cosmetic nits (trailing newlines, whitespace, import ordering, line length, quote style)
  with no functional effect — formatters handle these.

If there are no actionable findings for a file, say nothing about that file.

## Change Summary in the Review Summary

Every review summary **must** open with a reviewer-written restatement of the PR's **intent** (what
it is trying to accomplish) and **broad implementation strategy** (how it goes about it), in 2–4
sentences. Many PRs in this project are AI-generated and the reviewer's independent restatement is
the most valuable thing the human merger receives — it surfaces mismatches between what the PR
description claims and what the diff actually does.

Constraints on the change summary:

- Describe, do not evaluate. No praise, no criticism, no "well-structured" or "could be cleaner".
  Defects belong in inline comments and the verdict / general feedback section.
- Be specific. "Adds a new feature" is useless; "Adds a `/notes/search` endpoint that issues a
  pgvector similarity query against `notes.embedding` and returns the top 10 by cosine distance" is
  useful.
- Stay grounded in the diff, not the PR description. If the description says X but the diff does Y,
  restate Y — that mismatch is exactly what the human merger needs to see.

## Tests and Linters Are Authoritative

CI runs `ruff`, `basedpyright`, `pylint`, `ast-grep`, `mdformat`, and the full test suite
deterministically on every revision. Reviewers — human or automated — should treat these as
authoritative and **must not** post comments speculating about whether something will fail tests or
fail a linter. Either point at the concrete defect in the code itself, or say nothing. CI results
are verified separately.

This frees review attention for what CI cannot check: correctness against intent, design, security,
and whether the change does the right thing.

## Core Principle: Fail Fast, Never Mask Errors

**Always prefer throwing an error to a graceful fallback.** This is one of the most important
principles to enforce during review — graceful fallbacks routinely mask real bugs, produce confusing
downstream symptoms, and cause more problems than they solve. See
[docs/development/error-handling.md](docs/development/error-handling.md) for the full rationale.

**Block** the following patterns as `ERROR_HANDLING_ISSUE` or `LOGIC_ERROR`:

- Catching an exception and returning `None`, `[]`, `{}`, `False`, `""`, or any other "empty"
  sentinel in place of the real result.
- `try / except Exception:` (or bare `except:`) wrapping logic whose failure modes have not been
  individually considered. Catch only the specific exceptions you know how to handle.
- Logging an error and continuing as if nothing happened. Logging is not handling — the caller still
  gets a wrong result.
- `if thing is None: return` guards inserted to suppress an error rather than to model a genuine
  optional case.
- "Graceful degradation" that silently swaps in a default value when the real value could not be
  produced.
- Re-raising without `from e`, which discards the original error chain.

**Require instead**:

- Let the exception propagate to a layer that actually has the context to handle it, or raise a more
  specific exception that preserves `__cause__` via `raise NewError(...) from e`.
- Validate inputs at system boundaries (HTTP handlers, message handlers, CLI entry points) and
  reject bad input with a clear error — do not paper over it deeper in the stack.
- Ask "is it **correct** to continue?" not "is it **possible** to continue?" If continuing produces
  a wrong answer, stop.

When a PR introduces a fallback the author argues is intentional, the burden of proof is on the
fallback: the review comment should demand a specific justification (what failure mode, why is a
wrong answer better than an exception, how will the caller know degradation occurred) and block the
change until that justification exists in the code or commit message.

## Proportionality and Accepted Trade-offs

Review demands must be proportionate to the scenario they protect. Sensible scenarios must have good
behavior; obscure scenarios need *reasonable* behavior, not ideal behavior. This applies with
special force to design-document reviews and to findings about races, TOCTOU windows, and exotic
misuse — the categories where iterative review most easily accretes machinery whose complexity is
not justified by the scenarios it perfects.

This is the review-side expression of the behaviour-altitude principle in `AGENTS.md`: common,
reasonable scenarios should get ideal behavior; uncommon or unusual scenarios should get reasonable
(correct, non-broken) behavior, not necessarily ideal behavior. Do not build elaborate machinery to
give rare scenarios ideal behavior when reasonable behavior suffices.

- **Weigh exposure before demanding machinery.** Ask who can actually observe the bad outcome. A
  race in which a user can only affect their own data through their own actions (e.g. racing their
  own reconnect of an account) does not justify leases, drain barriers, or generation fencing. A
  cross-user or attacker-facing hole does — flag those regardless of how narrow the window is.
- **Apply a cost/benefit gate before adding machinery.** Before pushing for significant new state,
  abstraction, or defensive machinery, state the concrete user-facing requirement it serves and
  confirm that the added complexity is proportionate to the benefit. Prefer the simplest design that
  satisfies the requirement, and prefer centralizing a policy over distributing it across layers.
  Reviewers must apply the same test: does the cost exceed the user-facing benefit?
- **Treat machinery-edge-case findings as a design signal.** When review keeps finding new edge
  cases inside defensive machinery rather than in real user-facing behavior, step back and simplify
  or centralize the design. Do not patch each machinery edge case with more state.
- **Machinery vs. a sentence.** When the honest resolution is to document an accepted residual
  behavior, prefer requiring that documentation over requiring new synchronization, classification,
  or plumbing. A *silent* gap is a finding; a documented, reasoned acceptance is not.
- **Respect documented deliberate simplifications.** If the change (or its design doc) explicitly
  records an accepted trade-off with rationale — for example a "Deliberate simplifications" section
  — do not re-flag it as a defect. Comment only if the stated rationale is factually wrong (e.g. the
  exposure is claimed to be self-only but actually crosses users).
- **Operator sovereignty over security trade-offs.** This deployment's security posture is
  operator-owned (`tools_policy`, `operator_minimum`, taint policy mode). Prefer safe-by-default
  with an explicit, logged operator override to unconditional gates; do not demand that a feature
  hard-refuse configurations an informed operator may legitimately choose. Do flag *silent* insecure
  defaults — the difference is whether the operator made a visible, deliberate choice.

## Severity Levels

### BREAKS_BUILD

**Exit Code Impact: 2 (blocking)**

Code that will prevent the application from building or starting:

- Syntax errors (missing brackets, incorrect indentation in Python)
- Import/require statements for non-existent modules
- Undefined variables or functions being called
- Type errors that would be caught at compile/startup time
- Missing required configuration or environment variables

Examples:

- `import nonexistent_module`
- `print(undefined_variable)`
- Missing closing parenthesis or bracket
- Incorrect indentation in Python code blocks

### RUNTIME_ERROR

**Exit Code Impact: 2 (blocking)**

Code that will crash or fail during execution:

- Null/None pointer access without checks
- Array index out of bounds
- Unhandled exceptions in critical paths
- Division by zero without guards
- Incorrect type assumptions (e.g., calling string methods on integers)
- Resource leaks (unclosed files, connections)

Examples:

- `user.name` without checking if user is None
- `items[index]` without bounds checking
- Missing try/except around external API calls
- `open()` without corresponding close or context manager

### SECURITY_RISK

**Exit Code Impact: 2 (blocking)**

Vulnerabilities that could be exploited:

- SQL injection vulnerabilities (string concatenation in queries)
- Command injection risks
- Hardcoded secrets, passwords, or API keys
- Unsafe deserialization
- Path traversal vulnerabilities
- Missing authentication/authorization checks
- Exposed sensitive information in logs
- Use of deprecated cryptographic functions

Examples:

- `query = f"SELECT * FROM users WHERE id = {user_input}"`
- `API_KEY = "sk-1234567890"`
- `eval(user_input)`
- Logging passwords or tokens

#### Project Threat Model

This project operates under a relaxed threat model with specific security assumptions:

Any issue proposed as a security risk **must** include an explicit threat-model analysis: who is the
attacker, what trust boundary is crossed, and why the issue is reachable and exploitable under this
project's actual architecture. That architecture uses trusted clients, server-enforced
authentication on every request, and a family/small-group model. If no attacker gains anything under
that model, the issue is not a security issue; classify it at most as correctness, UX, or cleanup. A
security label without a demonstrated threat does not make something a security issue.

**API Endpoint Authentication:**

- All `/api/*` endpoints require authentication (session or API token)
- The server, not client-side state on a trusted client, is the authentication boundary
- Authorization (user-specific access control) is relaxed between authenticated users
- This provides security while maintaining simplicity for family/small group use

**Inter-User Security:**

- The system doesn't implement strict isolation between authenticated users
- All authenticated users can access most resources (notes, attachments, etc.)
- This is acceptable for the family/small group use case this application targets

**When NOT to flag as SECURITY_RISK:**

- Lack of user-specific authorization checks between authenticated users
- Shared access to attachments or other resources between authenticated users
- Simplified permission models for family/small group scenarios
- Client-side auth state on a trusted client when the server rejects it. For example, a rejected or
  expired token lingering in a trusted client's keychain is not exploitable because the server
  rejects it; it is at most correctness, UX, or cleanup debt. A still-valid token surviving logout
  on a shared device between mutually untrusted users would be a security concern, but that is not
  this application's personal-device model.

**When TO flag as SECURITY_RISK:**

- Missing authentication on any endpoints (all endpoints should require auth)
- Hardcoded secrets or credentials
- SQL injection or command injection vulnerabilities
- Logging sensitive information like passwords or tokens
- Unsafe handling of user input or file uploads

### LOGIC_ERROR

**Exit Code Impact: 2 (blocking)**

Incorrect program logic that produces wrong results:

- Wrong conditional logic (using AND instead of OR)
- Off-by-one errors in loops
- Incorrect comparison operators
- Missing edge case handling
- Race conditions in concurrent code
- Incorrect state transitions
- Wrong algorithm implementation
- Silent failures (catch-all blocks that swallow errors)
- Graceful fallbacks that mask underlying issues (returning None/empty on error)

Examples:

- `try: ... except: return None` (swallowing errors)
- `if x > 10 and x < 5:` (impossible condition)
- `for i in range(len(items) + 1):` (will go out of bounds)
- Missing handling for empty lists or None values

### ERROR_HANDLING_ISSUE

**Exit Code Impact: 2 (blocking)**

See **[docs/development/error-handling.md](docs/development/error-handling.md)** for comprehensive
guidelines.

Improper error handling that masks bugs, creates confusing errors, or silently loses data:

- **Silent failures**: Catching exceptions and returning null/empty (logging alone is still
  silent—the user doesn't see the log)
- **Cascading silent failures**: Silent failure at one layer causes confusing error at another
  (e.g., video silently stripped → "empty input" error shown to user)
- **Catch-and-return-null**: Catching broad exceptions and returning None, making it impossible to
  distinguish "not found" from "error occurred"
- **Overly broad exception handling**: Catching `Exception` when specific exception types should be
  caught
- **Lost error context**: Re-raising exceptions without preserving the original error chain
- **"Graceful degradation" that masks bugs**: Returning default values that produce confusing
  downstream behavior

**Key principle**: Ask "is it **correct** to continue?" not "is it **possible** to continue?"

Examples:

```python
# BAD: Silent failure - user sends video, gets "empty input" error
def extract_content(msg):
    try:
        return msg.video
    except Exception:
        return None  # Video silently dropped!

# BAD: Catch-and-return-null masks bugs
def get_user(id):
    try:
        return db.query(User).get(id)
    except Exception:
        return None  # Was it not found, or did DB crash?

# BAD: Overly broad exception handling
try:
    result = complex_operation()
except Exception:
    return default_value  # Hides bugs in complex_operation()
```

```python
# GOOD: Catch specific exceptions, let others propagate
def get_user(id) -> User | None:
    try:
        return db.query(User).get(id)
    except NoResultFound:
        return None  # Expected case
    # Other exceptions propagate - infrastructure or bug

# GOOD: Preserve error context
try:
    external_api.call()
except ExternalAPIError as e:
    raise ProcessingError(f"Failed: {e}") from e
```

### DESIGN_FLAW_MAJOR

**Exit Code Impact: 2 (blocking)**

Significant architectural issues requiring substantial refactoring:

- Circular dependencies between modules
- God objects/functions doing too much
- Tight coupling preventing testability
- Wrong abstraction level
- Synchronous operations that should be async
- Missing critical error handling patterns
- Database queries in loops (N+1 problem)
- Use of mutable global state outside of the application's main entry point
- Instantiation of objects with non-trivial external dependencies deep in the call stack (violates
  Dependency Injection)

Examples:

- Class with 20+ methods handling unrelated concerns
- Direct database access in view/controller layers
- Hardcoded business logic that should be configurable

### DESIGN_FLAW_MINOR

**Exit Code Impact: 1 (warning)**

Local design issues that should be addressed but won't break functionality:

- Functions doing more than one thing
- Poor naming that obscures intent
- Missing appropriate abstractions
- Code duplication that could be refactored
- Inconsistent patterns within a module
- Missing dependency injection

Examples:

- Function named `processData` that also sends emails
- Copy-pasted code blocks with minor variations
- Mixed responsibility in a single class

### SHORTCUT

**Exit Code Impact: 1 (warning)**

This applies when a small part of a task is deliberately left unfinished, as opposed to a larger
feature that is still under development.

Examples:

- A migration to support multiple items finds another area that needs to be updated, but the code is
  updated to just take the first item from a list for now.
- Including "backwards compatibility" code for internal refactorings instead of updating all call
  sites (this prevents the type checker from finding broken usage).
- A linter warning is disabled without a good reason, simply to make the linter pass.
- A TODO comment is added for something that should be handled in the current change.

### BEST_PRACTICE

**Exit Code Impact: 1 (warning)**

Deviations from established patterns and conventions:

- Missing docstrings/comments for complex logic
- Not following project naming conventions
- Missing type hints (in typed codebases)
- Overuse of `Any` or `dict[str, Any]` for structured data
- Not using context managers for resources
- Ignored linter warnings
- Missing error context in exception handling
- Using mutable default arguments

**Type Safety Guidance:**

- If a dictionary has a known structure (e.g. `{"monkeys": {"like": "bananas"}}`), it **must** be
  typed using `TypedDict`, `dataclass`, or Pydantic model.
- `dict[str, Any]` is only acceptable when the structure is genuinely unknown or dynamic.
- Recommend finding or creating a correct type instead of using `Any`.

Examples:

- `def calculate_total(items=[]):` # mutable default
- `def process(data: dict[str, Any]):` where data has a known structure (use TypedDict/Pydantic
  instead)
- Catching broad exceptions: `except Exception:`
- Magic numbers without constants

### STYLE

**Exit Code Impact: 0 (pass)**

Code formatting and style issues:

- Inconsistent indentation or spacing
- Line length violations
- Import ordering issues
- Trailing whitespace
- Missing blank lines between functions
- Inconsistent quote usage

Examples:

- Mixing tabs and spaces
- Lines longer than project limit
- Unsorted imports

### SUGGESTION

**Exit Code Impact: 0 (pass)**

Improvements for better code quality:

- Performance optimizations
- More idiomatic code patterns
- Alternative approaches worth considering
- Opportunities to use standard library better
- Documentation improvements
- Test coverage suggestions

Examples:

- "Consider using list comprehension instead of loop"
- "This could be simplified using `collections.defaultdict`"
- "Adding unit tests for edge cases would improve coverage"

## Review Process

The review system will:

1. Analyze the git diff for changes
2. Categorize each issue found by severity
3. Exit with the highest severity level found
4. Provide actionable feedback for improvements

## Project-Specific Patterns

This project follows the patterns defined in CLAUDE.md:

- No self-evident comments
- All code must pass linting (ruff, basedpyright, pylint)
- Use virtualenv in .venv
- Follow existing code conventions in the codebase

## Linter and Type-Checker Bypasses

**Severity: 🟠 High / SHORTCUT — without a specific justification.** Reviewers must flag this at high
priority. The exit-code mapping is `SHORTCUT` (warning), but the review-comment severity is high: an
unjustified bypass routinely hides a real defect, and waving it through trains the team to suppress
more warnings.

Any bypassing of type-checkers, ast-grep rules, ruff, pylint, or other linters must be accompanied
by a **specific** inline justification comment explaining the underlying problem the bypass works
around. Generic justifications such as "needed", "false positive", "fixes lint", "to make CI pass",
or "linter is wrong" are **not acceptable** — flag them as if no justification were present.

### Common Bypass Patterns

All of these patterns require justification:

- `# noqa: <code>` - Ruff/flake8 warning suppression
- `# type: ignore` - Type checker suppression (mypy, basedpyright, pyright)
- `# pylint: disable=<rule>` - Pylint warning suppression
- `# ast-grep-ignore` - ast-grep rule bypass
- `# pragma: no cover` - Test coverage exclusion

### Justification Requirements

The justification should:

1. **Be specific** - Explain the exact reason for the bypass
2. **Be inline** - Appear on the same line or immediately before the bypass
3. **Reference the issue** - Mention what problem necessitates the bypass

### Examples

❌ **Unacceptable - no justification:**

```python
result = cast(str, value)  # type: ignore
```

```python
# noqa: S603
subprocess.run(command, shell=True)
```

✅ **Acceptable - clear justification:**

```python
# SQLAlchemy's Column.label() is incorrectly typed; it returns ColumnElement but should return Label
result = cast(str, value)  # type: ignore[arg-type]
```

```python
# Command is constructed from trusted configuration, not user input - S603 subprocess call with shell=True
subprocess.run(command, shell=True)  # noqa: S603
```

```python
# ast-grep incorrectly flags this as asyncio.sleep in test, but it's actually time.sleep
time.sleep(0.1)  # ast-grep-ignore: no-asyncio-sleep-in-tests
```

### Review Process

When reviewing code with linter bypasses:

1. **Check for justification** - If missing, flag as **SHORTCUT**
2. **Evaluate necessity** - Is the bypass actually needed or can the code be refactored?
3. **Verify specificity** - Generic justifications like "needed" or "false positive" are
   insufficient

### When to Accept Without Justification

- Automatically generated code (e.g., by tools like `alembic`)
- Third-party code that shouldn't be modified
- Code marked with a project-wide exception pattern (document these separately)

## Auto-Formatting Tools

This project uses automatic code formatting tools. **The reviewer should NOT suggest style changes
that conflict with these tools**:

### Python Formatting

- **Tool**: `ruff format`
- **Configuration**: Automatic formatting with preview features enabled (`preview = true` in
  `[tool.ruff]`)
- **What it handles**: Indentation, spacing, line breaks, import ordering, quote consistency
- **Important**: Do NOT flag Python style issues that ruff would automatically fix

### Python Linting

- **Tool**: `ruff check --fix --ignore=E501`
- **Configuration**: Auto-fixes enabled, preview features enabled (`preview = true` in
  `[tool.ruff]`), line length (E501) ignored
- **What it handles**: Code style violations, unused imports, common errors
- **Important**: The reviewer should focus on logic and design issues, not style

### Markdown Formatting

- **Tool**: `mdformat --wrap 100`
- **Configuration**: `.mdformat.toml` with 100-character line wrap
- **What it handles**:
  - Consistent heading styles
  - List formatting and indentation
  - Line wrapping at 100 characters
  - Code block formatting
  - Table formatting
- **Important**: Do NOT flag Markdown style issues that mdformat would automatically fix

### Pre-commit Hooks

The project uses pre-commit hooks that run these formatters automatically:

- Python files: `ruff check --fix` and `ruff format` (preview enabled via `pyproject.toml`)
- Markdown files: `mdformat --wrap 100`

### Review Focus

Given the auto-formatting tools in place, the reviewer should focus on:

1. **Logic errors** and algorithmic correctness
2. **Security vulnerabilities** and safety issues
3. **Design patterns** and architecture decisions
4. **Performance issues** and efficiency concerns
5. **Missing error handling** or edge cases
6. **Documentation completeness** (not formatting)
7. **Test coverage** and quality

### Test Quality Requirements

#### Testing Philosophy: Prefer Real/Fake Dependencies Over Mocks

The most important goal when writing a test is to "bring the system into contact with reality" and
prove that it actually works. To ensure our tests are realistic and maintainable, we follow a
principle of using real or fake dependencies wherever possible.

- **Real Dependencies**: Use real components like a test database (e.g., via `db_engine`) or a real
  protocol client for the most accurate testing. This gives real assurance that the system is
  compatible with actual clients of the protocols we implement.
- **Fake Dependencies**: When a real service is not practical (e.g., it's slow or complex to set
  up), use a high-fidelity "fake" implementation or a simple in-memory version using official SDKs
  that mimic the real API and behavior.
- **Mocks**: Reserve mocks as a very last resort, primarily for simulating external third-party
  services that are truly impossible to control or fake.

**Rationale**:

- **Realism & Compatibility**: We want to guard against hallucinating or misunderstanding the
  interfaces we implement or consume. Integrating an external reference (like a real client or
  official SDK) provides higher confidence than a mock.
- **Avoiding Fragile "Change-Detectors"**: Unit tests often end up being fragile change-detectors.
  If an interface is misunderstood in the implementation, that same mistake is often repeated in a
  unit test's mocks, making the test "pass" while the system is actually broken.
- **Maintainability**: Mocks are brittle and require updates when a component's internal API
  changes, even if the external behavior hasn't. Fakes and real dependencies are generally more
  robust.

**Review Guidelines**:

- **Block** changes that over-rely on mocks instead of fakes or real systems. This is a
  **DESIGN_FLAW_MAJOR**.
- **Block** mocks for components that already have fake implementations or are easily testable with
  real instances (e.g., mocking the database).
- **Question** the use of unit tests that merely repeat the implementation logic with mocks.
- **Encourage** coding up simple in-memory implementations using official SDKs or shoehorning
  collaborators into test-friendly versions (as we do for Home Assistant and Calendar).
- **Accept** mocks only for external, third-party services where creating a fake is truly
  impractical.

#### Time-Based Waits

**CRITICAL: Tests must NEVER use fixed time-based waits.** This is a **LOGIC_ERROR** severity issue.

Tests using `setTimeout`, `sleep`, or fixed delays are flaky and will fail under load or in CI:

❌ **Block these patterns:**

- `await new Promise(resolve => setTimeout(resolve, 2000))`
- `await sleep(500)` (without condition check)
- `time.sleep(1.0)` (without condition check)
- Any arbitrary delay before checking a condition

✅ **Require these patterns:**

- `await waitFor(() => expect(element).toBeEnabled())`
- `await screen.findByText('Success message')`
- `while not condition and time.time() < deadline: await asyncio.sleep(0.1)`
- Condition-based waits with timeouts

**Rationale:**

- Fixed waits are always wrong: too short (flaky) or too long (slow)
- Condition-based waits complete as soon as the condition is met
- Tests must be reliable across different hardware and CI environments

**When reviewing test changes:**

1. Flag any new `setTimeout`, `sleep`, or fixed delays as **LOGIC_ERROR**
2. Suggest condition-based alternatives
3. Accept fixed waits ONLY if modeling actual user behavior timing
4. Ensure test helpers use condition-based waits internally

The reviewer should **NOT** focus on:

1. Code formatting (handled by ruff)
2. Markdown formatting (handled by mdformat)
3. Import ordering (handled by ruff)
4. Line length in Python (explicitly ignored with E501)
5. Whitespace or indentation issues

### Do not comment on

- Do NOT correct version numbers, dates and times, etc - remember that your training cutoff may be
  significantly before the present. For example, if a change references a version number that you
  don't know about or that you think is "still in beta", or has the year as 2025, which you believe
  to be in the future, do NOT comment. It's likely that you are wrong.
- Do NOT insist on backwards compatibility if a previous comment has indicated that there is nothing
  to be backwards-compatible with (e.g. changing a hash format where the commit message tells you
  that the hash has never been persisted).
- Do NOT make blocking comments for issues that have been explicitly acknowledged in the commit
  message, the design doc (e.g. a "Deliberate simplifications" section), or where a TODO has been
  left. Advise, don't block. See "Proportionality and Accepted Trade-offs" above.
- Do NOT correct library usage based on your knowledge - your information might be outdated. Library
  APIs evolve over time and the code may be using newer APIs that you're not aware of. That's what
  type checking and tests are for. Only flag library usage if you can demonstrate it's objectively
  wrong based on the imports and usage patterns within the codebase itself.
