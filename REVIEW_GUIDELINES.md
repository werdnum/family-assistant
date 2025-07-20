# Code Review Guidelines

This document defines the severity levels and review criteria used by the automated code review
system. When reviewing code changes, issues are categorized by their potential impact.

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

Examples:

- `if x > 10 and x < 5:` (impossible condition)
- `for i in range(len(items) + 1):` (will go out of bounds)
- Missing handling for empty lists or None values

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

### BEST_PRACTICE

**Exit Code Impact: 1 (warning)**

Deviations from established patterns and conventions:

- Missing docstrings/comments for complex logic
- Not following project naming conventions
- Missing type hints (in typed codebases)
- Not using context managers for resources
- Ignored linter warnings
- Missing error context in exception handling
- Using mutable default arguments

Examples:

- `def calculate_total(items=[]):` # mutable default
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

## Auto-Formatting Tools

This project uses automatic code formatting tools. **The reviewer should NOT suggest style changes
that conflict with these tools**:

### Python Formatting

- **Tool**: `ruff format --preview`
- **Configuration**: Automatic formatting with preview features enabled
- **What it handles**: Indentation, spacing, line breaks, import ordering, quote consistency
- **Important**: Do NOT flag Python style issues that ruff would automatically fix

### Python Linting

- **Tool**: `ruff check --fix --preview --ignore=E501`
- **Configuration**: Auto-fixes enabled, line length (E501) ignored
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

- Python files: `ruff check --fix --preview` and `ruff format --preview`
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

The reviewer should **NOT** focus on:

1. Code formatting (handled by ruff)
2. Markdown formatting (handled by mdformat)
3. Import ordering (handled by ruff)
4. Line length in Python (explicitly ignored with E501)
5. Whitespace or indentation issues
