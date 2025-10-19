# ast-grep Guide for This Project

This document provides comprehensive guidance for working with ast-grep rules in this project.

## Table of Contents

01. [Overview](#overview)
02. [How ast-grep is Used in This Project](#how-astgrep-is-used-in-this-project)
03. [Conformance Rules vs Hints](#conformance-rules-vs-hints)
04. [Pattern Syntax Reference](#pattern-syntax-reference)
05. [Writing New Rules](#writing-new-rules)
06. [Testing Rules](#testing-rules)
07. [Exemptions](#exemptions)
08. [Common Pitfalls and Solutions](#common-pitfalls-and-solutions)
09. [Script Reference](#script-reference)
10. [Resources](#resources)

## Overview

**ast-grep** is a structural code search and transformation tool that uses Abstract Syntax Tree
(AST) pattern matching. Unlike text-based tools like grep or sed, ast-grep understands code
structure, making it ideal for:

- Enforcing code quality standards (conformance rules)
- Providing helpful suggestions (hints)
- Making mechanical code transformations
- Finding complex code patterns

In this project, ast-grep is used for:

1. **Code conformance checking** - Enforcing banned patterns to prevent anti-patterns
2. **Code hints** - Suggesting best practices without blocking commits
3. **Code transformations** - Making large-scale mechanical changes

## How ast-grep is Used in This Project

### Integration Points

ast-grep rules are enforced at multiple points in the development workflow:

1. **Post-edit hooks** (`.claude/lint-hook.py`)

   - Runs automatically after Claude edits files
   - Shows conformance violations and hints
   - Provides immediate feedback

2. **Pre-commit hooks** (`.pre-commit-config.yaml`)

   - Blocks commits with new violations
   - Ensures conformance rules are respected

3. **CI/CD pipeline** (GitHub Actions)

   - Runs as part of the test suite
   - Prevents merging code with violations

4. **Linting suite** (`poe lint`)

   - Includes conformance checking
   - Part of the full code quality check

5. **Manual checks** (`scripts/check-conformance.sh`)

   - On-demand conformance checking
   - Useful for debugging and auditing

### Directory Structure

```
.ast-grep/
├── rules/                      # Conformance rules (severity: error)
│   ├── no-asyncio-sleep-in-tests.yml
│   ├── no-time-sleep-in-tests.yml
│   ├── no-playwright-wait-for-timeout.yml
│   └── hints/                  # Hint rules (severity: hint)
│       ├── async-magic-mock.yml
│       └── test-mocking-guideline.yml
├── tests/                      # Test infrastructure (planned)
│   ├── conformance/           # Tests for conformance rules
│   └── hints/                 # Tests for hint rules
├── check-conformance.py       # Main conformance checker with exemptions
├── check-hints.py             # Hints checker (never fails)
├── exemptions.yml             # File-level exemptions
├── EXEMPTIONS.md              # Exemptions guide
└── CLAUDE.md                  # This file

sgconfig.yml                   # ast-grep project configuration (in repo root)
```

## Conformance Rules vs Hints

### Conformance Rules

**Purpose**: Enforce code quality by banning anti-patterns

**Characteristics**:

- Located in `.ast-grep/rules/`
- `severity: error`
- Block commits when violated
- Can be exempted with inline comments or exemptions.yml
- Used to prevent bugs and maintainability issues

**Examples**:

- `no-asyncio-sleep-in-tests` - Prevents flaky tests
- `no-time-sleep-in-tests` - Prevents blocking the event loop
- `no-playwright-wait-for-timeout` - Prevents fragile UI tests

**When to add**:

- Pattern reliably indicates a problem
- Alternative approach exists
- Team agrees the pattern should be banned

### Hints

**Purpose**: Provide helpful suggestions and best practices

**Characteristics**:

- Located in `.ast-grep/rules/hints/`
- `severity: hint`
- Never block commits or fail builds
- Purely informational
- Used for education and awareness

**Examples**:

- `async-magic-mock` - Suggests using AsyncMock in async functions
- `test-mocking-guideline` - Reminds about preferring real objects over mocks

**When to add**:

- Pattern suggests a potential improvement
- There are legitimate exceptions
- Want to raise awareness without enforcing

## Pattern Syntax Reference

### Fundamentals

ast-grep patterns must be **valid, parseable code** in the target language. They match AST nodes,
not text.

```python
# ✅ Valid pattern - this is valid Python
pattern: asyncio.sleep($$$)

# ❌ Invalid pattern - not valid Python syntax
pattern: asyncio.sleep(  # Missing closing paren
```

### Meta-Variables

Meta-variables are placeholders that match code elements:

| Meta-Variable | Matches                             | Example                                           |
| ------------- | ----------------------------------- | ------------------------------------------------- |
| `$VAR`        | Single AST node                     | `$VAR = 42` matches `x = 42`                      |
| `$$$ARGS`     | Zero or more nodes                  | `func($$$ARGS)` matches `func()` and `func(1, 2)` |
| `$_`          | Anonymous (match but don't capture) | `func($_)` matches any single argument            |

**Naming rules**:

- Must start with `$`
- Followed by uppercase letters, underscores, or digits
- Valid: `$VAR`, `$META_VAR1`, `$_`, `$123`
- Invalid: `$invalid`, `$Svalue` (lowercase after $)

**Examples**:

```yaml
# Match any function call
pattern: $FUNC($$$ARGS)

# Match variable assignment
pattern: $VAR = $VALUE

# Match method call on any object
pattern: $OBJ.$METHOD($$$ARGS)

# Match specific argument pattern
pattern: requests.get($URL, timeout=$_)
```

### Unnamed Nodes and Keywords

Some AST nodes don't have named representations (e.g., `async` keyword in Python). To match these:

**Option 1: Use `context` and `selector`**

```yaml
# Match async function definitions
rule:
  pattern:
    context: 'async def $FUNC($$$ARGS): $$$BODY'
    selector: function_definition
```

The `context` provides the full pattern including unnamed nodes, and `selector` specifies which node
type to match.

**Option 2: Use `kind` with other constraints**

```yaml
# Match all function definitions inside a class
rule:
  kind: function_definition
  inside:
    kind: class_definition
```

### Relational Operators

Combine patterns using logical operators:

#### `all` - Match if ALL conditions are true

```yaml
rule:
  all:
    - pattern: requests.get($$$ARGS)
    - not:
        has:
          pattern: timeout = $_
```

Matches `requests.get()` calls that DON'T have a timeout argument.

#### `any` - Match if ANY condition is true

```yaml
rule:
  any:
    - pattern: time.sleep($$$)
    - pattern: asyncio.sleep($$$)
```

Matches either `time.sleep()` or `asyncio.sleep()`.

#### `not` - Negate a condition

```yaml
rule:
  pattern: $FUNC($$$ARGS)
  not:
    has:
      pattern: cache = $_
```

Matches function calls that DON'T have a `cache` argument.

#### `has` - Check for presence of sub-pattern

```yaml
rule:
  pattern: my_function($$$ARGS)
  has:
    pattern: debug = True
```

Matches `my_function()` calls that include `debug=True`.

#### `inside` - Check if node is within another

```yaml
rule:
  pattern: asyncio.sleep($$$)
  not:
    inside:
      kind: while_statement
```

Matches `asyncio.sleep()` calls NOT inside while loops.

### Pattern Strictness

ast-grep supports different matching strictness levels:

- `cst` - Concrete syntax tree (exact match including whitespace)
- `smart` - Default, ignores trivia (whitespace, comments)
- `ast` - Abstract syntax tree (ignores syntactic details)
- `relaxed` - More flexible matching
- `signature` - Matches function signatures

**In practice**, use the default `smart` matching unless you need exact matches.

### Common Patterns for Python

```yaml
# Match function definitions
pattern: def $FUNC($$$ARGS): $$$BODY

# Match method calls
pattern: $OBJ.$METHOD($$$ARGS)

# Match imports
pattern: from $MODULE import $NAME

# Match class definitions
pattern: class $CLASS($$$BASES): $$$BODY

# Match async function definitions (use context/selector)
pattern:
  context: 'async def $FUNC($$$ARGS): $$$BODY'
  selector: function_definition

# Match with expressions
pattern: with $CONTEXT as $VAR: $$$BODY

# Match try/except blocks
pattern: |
  try:
    $$$TRY_BODY
  except $$$EXCEPTIONS:
    $$$EXCEPT_BODY

# Match list comprehensions
pattern: '[$EXPR for $VAR in $ITER]'
```

## Writing New Rules

### Rule File Structure

All rules follow this basic structure:

```yaml
id: rule-identifier               # Unique ID (kebab-case)
language: python                  # Target language
severity: error                   # 'error' for conformance, 'hint' for suggestions
message: "Short description"      # One-line summary shown to user
note: |                          # Detailed explanation (multi-line)
  Longer explanation of why this pattern is problematic.
  
  Show examples of the problem and the solution.
  
  Explain any exemptions or edge cases.
rule:                            # The actual matching rule
  pattern: banned_function($$$)  # Simple pattern
  # OR complex rule with operators:
  all:
    - pattern: some_function($$$)
    - not:
        has:
          pattern: required_arg = $_
```

### Step-by-Step: Creating a Conformance Rule

**1. Identify the anti-pattern**

Example: We want to ban `eval()` in production code because it's a security risk.

**2. Choose a rule ID**

Use kebab-case: `no-eval-in-production`

**3. Write the pattern**

Start simple:

```yaml
pattern: eval($$$)
```

Test it:

```bash
ast-grep -p 'eval($$$)' -l python src/
```

**4. Add constraints if needed**

If we want to allow it in tests:

```yaml
rule:
  pattern: eval($$$)
  # Implemented in check-conformance.py: only runs on src/ files
```

Or use `inside`/`not` to be more specific:

```yaml
rule:
  pattern: eval($$$)
  not:
    inside:
      kind: function_definition
      pattern: def test_$$$(): $$$BODY
```

**5. Create the rule file**

Create `.ast-grep/rules/no-eval-in-production.yml`:

```yaml
id: no-eval-in-production
language: python
severity: error
message: "Avoid eval() - it's a security risk"
note: |
  eval() executes arbitrary Python code and is a security vulnerability.
  
  Instead, use safer alternatives:
  
  # ❌ Bad - security risk
  result = eval(user_input)
  
  # ✅ Good - use ast.literal_eval for safe evaluation
  import ast
  result = ast.literal_eval(user_input)
  
  # ✅ Good - use a parser for specific use cases
  result = parse_expression(user_input)
  
  For exemptions (e.g., REPL implementation):
    # ast-grep-ignore: no-eval-in-production - REPL needs eval for evaluation
    result = eval(code)
rule:
  pattern: eval($$$)
```

**6. Test the rule**

```bash
# Test on specific files
ast-grep scan --rule .ast-grep/rules/no-eval-in-production.yml src/

# Test with the full checker
.ast-grep/check-conformance.py src/
```

**7. Handle existing violations**

If there are existing violations that can't be fixed immediately, add exemptions:

```yaml
# .ast-grep/exemptions.yml
exemptions:
  - rule: no-eval-in-production
    files:
      - src/repl/evaluator.py
    reason: "REPL implementation requires eval for code evaluation"
    ticket: null
```

**8. Update test-only rules list** (if applicable)

If the rule should only apply to test files, add it to `check-conformance.py`:

```python
# Rules that only apply to test files
test_only_rules = {
    "no-asyncio-sleep-in-tests",
    "no-time-sleep-in-tests",
    "no-playwright-wait-for-timeout",
    "no-eval-in-production",  # Add here if it's test-specific
}
```

**9. Document the rule**

Update `.ast-grep/rules/README.md`:

```markdown
#### `no-eval-in-production`

**Pattern**: `eval($$$)` in production code

**Why it's banned**: eval() is a security vulnerability that executes arbitrary code.

**Replacement**:
- Use `ast.literal_eval()` for safe evaluation of literals
- Use domain-specific parsers for structured input
- Use `compile()` with restricted globals for safe code execution
```

**10. Commit the rule**

```bash
git add .ast-grep/rules/no-eval-in-production.yml
git add .ast-grep/rules/README.md
git add .ast-grep/exemptions.yml  # If you added exemptions
git commit -m "Add no-eval-in-production conformance rule"
```

### Step-by-Step: Creating a Hint Rule

Hints follow the same process but:

1. Save to `.ast-grep/rules/hints/` directory
2. Use `severity: hint` instead of `severity: error`
3. Add to test-only list in `check-hints.py` if needed
4. No need to document in README.md (hints are opt-in information)

**Example hint**:

```yaml
# .ast-grep/rules/hints/prefer-pathlib.yml
id: prefer-pathlib
language: python
severity: hint
message: "Consider using pathlib.Path instead of os.path"
note: |
  pathlib provides a more modern, object-oriented interface for file paths.
  
  # ⚠️ Old style
  path = os.path.join(dir, filename)
  if os.path.exists(path):
      with open(path) as f:
          data = f.read()
  
  # ✅ Modern style
  path = Path(dir) / filename
  if path.exists():
      data = path.read_text()
  
  This is a suggestion, not a requirement. os.path is still valid.
rule:
  any:
    - pattern: os.path.$METHOD($$$)
    - pattern: os.path.join($$$)
```

### Advanced Rule Patterns

#### Matching Order-Independent Arguments

Use `has` to match arguments regardless of order:

```yaml
rule:
  all:
    - pattern: my_function($$$)
    - has:
        pattern: required_arg = $_
    - has:
        pattern: another_arg = $_
```

#### Matching Nested Structures

```yaml
# Match nested function calls
rule:
  pattern: outer($$$, inner($$$), $$$)
```

#### Excluding Specific Contexts

```yaml
# Ban pattern except in specific functions
rule:
  pattern: dangerous_operation($$$)
  not:
    inside:
      pattern: def safe_wrapper($$$): $$$BODY
```

#### Multiple Patterns with Different Fixes

Use inline rules for transformations:

```bash
ast-grep -U --inline-rules '
- id: fix-old-api
  language: python
  rule: {pattern: old_api($ARGS), fix: "new_api($ARGS)"}
- id: fix-deprecated
  language: python
  rule: {pattern: deprecated_func($ARGS), fix: "modern_func($ARGS)"}
' .
```

## Testing Rules

### Manual Testing

**Test a rule file:**

```bash
# Scan specific files
ast-grep scan --rule .ast-grep/rules/my-rule.yml src/

# Scan all files
ast-grep scan --rule .ast-grep/rules/my-rule.yml

# Test with pattern directly
ast-grep -p 'pattern_here' -l python src/
```

**Test with the full checker:**

```bash
# Test conformance rules
.ast-grep/check-conformance.py src/

# Test hints
.ast-grep/check-hints.py src/

# Test specific files
.ast-grep/check-conformance.py src/specific_file.py
```

**Test inline rules:**

```bash
ast-grep --inline-rules '
id: test-rule
language: python
rule:
  pattern: test_pattern($$$)
' src/
```

### Automated Testing (Planned)

The project will have a test infrastructure in `.ast-grep/tests/`:

```
.ast-grep/tests/
├── conformance/
│   └── no-asyncio-sleep-in-tests/
│       ├── valid.py       # Should NOT match
│       ├── invalid.py     # Should match
│       └── test.yml       # Test definition
└── hints/
    └── async-magic-mock/
        ├── valid.py
        ├── invalid.py
        └── test.yml
```

**Test definition format:**

```yaml
# test.yml
rule: no-asyncio-sleep-in-tests
valid:
  - valid.py
invalid:
  - invalid.py
```

**Test runner** (planned):

```bash
# Run all rule tests
pytest .ast-grep/tests/

# Run specific rule test
pytest .ast-grep/tests/conformance/no-asyncio-sleep-in-tests/
```

### Interactive Testing

**Use the ast-grep playground:**

1. Go to https://ast-grep.github.io/playground.html
2. Select "Python" as language
3. Enter your pattern in the "Pattern" box
4. Enter test code in the "Source" box
5. See matches highlighted in real-time

**Use ast-grep CLI for exploration:**

```bash
# Show AST structure
ast-grep -p 'async def $FUNC($$$): $$$' --debug-query=ast

# Show matched nodes
ast-grep -p 'asyncio.sleep($$$)' -l python tests/ --json
```

## Exemptions

### Types of Exemptions

**1. Inline exemptions** - For single lines:

```python
# ast-grep-ignore: rule-id - Reason why exemption is needed
problematic_code()
```

**2. Block exemptions** - For multiple lines:

```python
# ast-grep-ignore-block: rule-id - Reason
def function_with_multiple_violations():
    problematic_code_1()
    problematic_code_2()
# ast-grep-ignore-end
```

**3. File-level exemptions** - In `.ast-grep/exemptions.yml`:

```yaml
exemptions:
  - rule: rule-id
    files:
      - path/to/file.py
      - path/to/*.py
    reason: "Explanation of why exemption is needed"
    ticket: "#123"  # Optional tracking ticket
```

### When to Use Exemptions

**Use exemptions when**:

- The spirit of the rule doesn't apply to this specific case
- There's no practical alternative
- Code legitimately needs the banned pattern

**DON'T use exemptions to**:

- Avoid fixing code that should be fixed
- Bypass code review
- Hide technical debt without a plan

### Exemption Best Practices

1. **Be specific**: Explain exactly why the exemption is needed
2. **Add context**: Reference tickets, requirements, or constraints
3. **Prefer narrow scope**: Inline > block > file-level
4. **Review regularly**: Audit exemptions periodically
5. **Track removal**: Include ticket numbers for future cleanup

**Good exemption**:

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - wait_for_condition() implementation itself
await asyncio.sleep(poll_interval)
```

**Bad exemption**:

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests
await asyncio.sleep(2)  # No reason provided!
```

For comprehensive exemption guidance, see [.ast-grep/EXEMPTIONS.md](EXEMPTIONS.md).

## Common Pitfalls and Solutions

### Pitfall 1: Pattern is Not Valid Syntax

**Problem**:

```yaml
# ❌ Invalid - not valid Python
pattern: async def  # Missing function name and body
```

**Solution**:

Use complete, valid syntax with meta-variables:

```yaml
# ✅ Valid
pattern: 'async def $FUNC($$$ARGS): $$$BODY'
```

### Pitfall 2: Trying to Match Unnamed Nodes Directly

**Problem**:

```yaml
# ❌ Won't work - 'async' is unnamed in the AST
pattern: async
```

**Solution**:

Use `context` and `selector`:

```yaml
# ✅ Works
pattern:
  context: 'async def $FUNC($$$ARGS): $$$BODY'
  selector: function_definition
```

Or use `kind`:

```yaml
# ✅ Alternative
rule:
  kind: function_definition
  # Add other constraints to identify async functions
```

### Pitfall 3: Too Broad Pattern

**Problem**:

```yaml
# ❌ Matches ALL function calls
pattern: $FUNC($$$)
```

**Solution**:

Be more specific:

```yaml
# ✅ Matches specific function
pattern: dangerous_function($$$)

# ✅ Or use constraints
rule:
  pattern: $FUNC($$$)
  inside:
    kind: test_function
```

### Pitfall 4: Forgetting to Handle Commas

**Problem**:

```yaml
# ❌ Doesn't handle trailing commas correctly
pattern: my_function($ARG1, cache=$_, $ARG2)
```

**Solution**:

Use multiple patterns to handle different positions:

```yaml
rule:
  any:
    - pattern: my_function($$$START, cache=$_, $$$END)
      fix: my_function($$$START, $$$END)
    - pattern: my_function(cache=$_, $$$END)
      fix: my_function($$$END)
    - pattern: my_function(cache=$_)
      fix: my_function()
```

See [docs/development/ast-grep-recipes.md](../../docs/development/ast-grep-recipes.md) for more
examples.

### Pitfall 5: Not Testing Enough Cases

**Problem**:

Only testing the pattern on one example.

**Solution**:

Test on:

- Different argument positions
- Different argument counts
- Edge cases (zero arguments, many arguments)
- Code with and without the pattern
- Different formatting (single line, multi-line)

```bash
# Test across the codebase
ast-grep -p 'your_pattern' -l python .

# Review all matches before committing
ast-grep scan --rule your-rule.yml
```

### Pitfall 6: Incorrect Meta-Variable Naming

**Problem**:

```yaml
# ❌ Invalid - lowercase after $
pattern: $myvar = $value
```

**Solution**:

Use uppercase:

```yaml
# ✅ Valid
pattern: $MY_VAR = $VALUE
```

### Pitfall 7: Expecting Text-Based Matching

**Problem**:

```yaml
# ❌ Won't match comments or strings
pattern: TODO
```

**Solution**:

ast-grep matches AST nodes, not text. For text-based matching, use grep:

```bash
# ✅ Use grep for text
grep -r "TODO" src/
```

### Pitfall 8: Rule ID Conflicts

**Problem**:

Two rules with the same ID cause unexpected behavior.

**Solution**:

Use unique, descriptive IDs:

```yaml
# ✅ Good IDs
id: no-asyncio-sleep-in-tests
id: prefer-pathlib-over-os-path
id: no-mutable-default-args
```

Follow the convention:

- Conformance: `no-banned-pattern` or `require-good-pattern`
- Hints: `prefer-better-pattern` or `consider-alternative`

## Script Reference

### check-conformance.py

**Purpose**: Main conformance checker with exemption support

**Usage**:

```bash
# Check all files
.ast-grep/check-conformance.py

# Check specific files
.ast-grep/check-conformance.py src/file.py

# JSON output
.ast-grep/check-conformance.py --json
```

**Features**:

- Loads rules from `.ast-grep/rules/*.yml`
- Supports inline, block, and file-level exemptions
- Filters test-only rules (only applies to `tests/` directory)
- Returns exit code 1 if violations found (blocks commits)

**Test-only rules** are configured in the script:

```python
test_only_rules = {
    "no-asyncio-sleep-in-tests",
    "no-time-sleep-in-tests",
    "no-playwright-wait-for-timeout",
}
```

Add new test-only rules to this set.

### check-hints.py

**Purpose**: Hints checker (never fails builds)

**Usage**:

```bash
# Check all files
.ast-grep/check-hints.py

# Check specific files
.ast-grep/check-hints.py src/file.py

# JSON output
.ast-grep/check-hints.py --json
```

**Features**:

- Loads rules from `.ast-grep/rules/hints/*.yml`
- Always returns exit code 0 (never blocks)
- Filters test-only hints
- Outputs with 💡 emoji for visibility

**Test-only hints** are configured in the script:

```python
test_only_hints = {
    "async-magic-mock",
    "test-mocking-guideline",
}
```

### check-conformance.sh

**Purpose**: Wrapper script for check-conformance.py

**Usage**:

```bash
# Same as check-conformance.py
scripts/check-conformance.sh [files...]
```

Changes to repo root before running.

### add-exemptions.py

**Purpose**: Automatically add inline ast-grep-ignore comments for rule violations

**Usage**:

```bash
# Add exemptions for all violations of a rule
.ast-grep/add-exemptions.py no-dict-any

# Add to specific files only
.ast-grep/add-exemptions.py no-dict-any src/file1.py src/file2.py

# Add to only changed files
.ast-grep/add-exemptions.py no-dict-any $(git diff --name-only)

# Preview without modifying
.ast-grep/add-exemptions.py no-dict-any --dry-run

# Custom exemption reason
.ast-grep/add-exemptions.py no-dict-any --reason "Legacy API requires untyped dict"
```

**Features**:

- Automatically finds violations using ast-grep scan
- Adds properly indented inline comments
- Skips violations that already have exemptions
- Supports dry-run mode for previewing changes
- Works with git to add exemptions only to changed files

**When to use**:

- After adding a new conformance rule with many existing violations
- When refactoring would take too long but you want to enforce the rule going forward
- To quickly exempt legacy code while planning proper fixes

**Important notes**:

- Always review changes before committing
- Ensure exemptions have meaningful reasons
- Use `--dry-run` first to preview what will change
- Run this on a clean working directory to avoid processing merge conflicts

### Integration with lint-hook.py

The `.claude/lint-hook.py` runs after Claude edits files:

- Runs `check-conformance.py` with `--json` flag
- Runs `check-hints.py` with `--json` flag
- Filters hints to only show new code (from Edit/Write tools)
- Displays violations with file and line numbers
- Shows auto-fix suggestions where applicable

## Resources

### Official ast-grep Documentation

- [Official Documentation](https://ast-grep.github.io/)
- [Pattern Syntax Guide](https://ast-grep.github.io/guide/pattern-syntax.html)
- [Rule Configuration Reference](https://ast-grep.github.io/guide/rule-config.html)
- [Python Catalog](https://ast-grep.github.io/catalog/python/) - Common Python patterns
- [Interactive Playground](https://ast-grep.github.io/playground.html)

### Project Documentation

- [Code conformance rules](rules/README.md) - Active rules and guidelines
- [Exemptions guide](EXEMPTIONS.md) - How to add and manage exemptions
- [ast-grep recipes](../docs/development/ast-grep-recipes.md) - Transformation examples
- [Testing guide](../tests/CLAUDE.md) - General testing philosophy

### Examples in This Project

Look at existing rules for examples:

**Conformance rules**:

- `.ast-grep/rules/no-asyncio-sleep-in-tests.yml` - Pattern with `not` and `inside`
- `.ast-grep/rules/no-time-sleep-in-tests.yml` - Multiple patterns with `any`
- `.ast-grep/rules/no-playwright-wait-for-timeout.yml` - Method call matching

**Hint rules**:

- `.ast-grep/rules/hints/async-magic-mock.yml` - Context-aware matching with `inside`
- `.ast-grep/rules/hints/test-mocking-guideline.yml` - Simple pattern with `any`

### Tips for Learning

1. **Start with simple patterns**: Test basic patterns before adding complexity
2. **Use the playground**: Validate patterns interactively at
   https://ast-grep.github.io/playground.html
3. **Study existing rules**: Learn from rules already in the project
4. **Test incrementally**: Add one constraint at a time and test
5. **Read the AST**: Use `--debug-query=ast` to understand AST structure
6. **Consult the catalog**: Check Python catalog for common patterns

### Getting Help

- **Ask in the project**: Questions about project-specific rules or policies
- **Check documentation**: Most questions are answered in the guides
- **Use the playground**: Test patterns interactively
- **Read existing rules**: See how similar patterns are handled
- **GitHub issues**: ast-grep repository for tool-specific questions

## Quick Reference

### Most Common Patterns

```yaml
# Match any function call
pattern: $FUNC($$$)

# Match specific function
pattern: specific_function($$$)

# Match method call
pattern: $OBJ.$METHOD($$$)

# Match with required argument
rule:
  pattern: my_function($$$)
  has:
    pattern: required_arg = $_

# Match without argument
rule:
  pattern: my_function($$$)
  not:
    has:
      pattern: excluded_arg = $_

# Match inside context
rule:
  pattern: risky_operation($$$)
  inside:
    pattern: def test_$$$(): $$$

# Match outside context
rule:
  pattern: risky_operation($$$)
  not:
    inside:
      pattern: def safe_wrapper($$$): $$$
```

### Essential Commands

```bash
# Test pattern
ast-grep -p 'pattern' -l python src/

# Scan with rule file
ast-grep scan --rule rules/my-rule.yml

# Check conformance
.ast-grep/check-conformance.py

# Check hints
.ast-grep/check-hints.py

# Apply transformation
ast-grep -U -p 'old($$$)' -r 'new($$$)' .

# Debug AST structure
ast-grep -p 'pattern' --debug-query=ast
```

### Rule File Template

```yaml
id: my-rule-id
language: python
severity: error  # or 'hint'
message: "Short description of what's banned or suggested"
note: |
  Detailed explanation of the rule.
  
  Show examples of problematic code and alternatives.
  
  Explain exemptions if needed.
rule:
  pattern: banned_pattern($$$)
  # OR complex rule:
  # all:
  #   - pattern: pattern($$$)
  #   - not:
  #       has:
  #         pattern: exception = $_
```
