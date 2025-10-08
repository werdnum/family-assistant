# ast-grep Transformation Recipes

This document provides recipe-style examples for using `ast-grep` to make mechanical code
transformations.

## Overview

`ast-grep` is the tool of choice for making mechanical syntactic changes in most cases.

**Note**: Use `ast-grep scan` for applying complex rule-based transformations (not `ast-grep run`).
The `scan` command supports YAML rule files and inline rules with `--inline-rules`.

## Recipe 1: Removing a Keyword Argument

**Task:** Reliably remove the `cache=...` keyword argument from all calls to `my_function`,
regardless of its position. *(This requires `--inline-rules` because a single pattern cannot handle
all comma variations.)*

**Before:**

```python
my_function(arg1, cache=True, other_arg=123)
my_function(cache=True, other_arg=123)
my_function(cache=True)
```

**Command:**

```bash
ast-grep -U --inline-rules '
id: remove-cache-kwarg-robust
language: python
rule:
  any:
    - pattern: my_function($$$START, cache=$_, $$$END)
      fix: my_function($$$START, $$$END)
    - pattern: my_function(cache=$_, $$$END)
      fix: my_function($$$END)
    - pattern: my_function(cache=$_)
      fix: my_function()
' .
```

**After:**

```python
my_function(arg1, other_arg=123)
my_function(other_arg=123)
my_function()
```

## Recipe 2: Changing Module Method to Instance Method

**Task:** Change calls from `mymodule.mymethod(object, ...)` to `object.mymethod(...)`. *(This is a
direct transformation suitable for the simpler `-p`/`-r` flags.)*

**Before:**

```python
my_instance = MyClass()
mymodule.mymethod(my_instance, 'arg1', kwarg='value')
```

**Command:**

```bash
ast-grep -U -p 'mymodule.mymethod($OBJECT, $$$ARGS)' -r '$OBJECT.mymethod($$$ARGS)' .
```

**After:**

```python
my_instance = MyClass()
my_instance.mymethod('arg1', kwarg='value')
```

## Recipe 3: Adding a Keyword Argument Conditionally

**Task:** Add `timeout=10` to `requests.get()` calls, but only if they don't already have one.
*(This requires `--inline-rules` to use the relational `not` and `has` operators.)*

**Before:**

```python
requests.get("https://api.example.com/status")
requests.get("https://api.example.com/data", timeout=5)
```

**Command:**

```bash
ast-grep -U --inline-rules '
id: add-timeout-to-requests-get
language: python
rule:
  pattern: requests.get($$$ARGS)
  not:
    has:
      pattern: timeout = $_
  fix: requests.get($$$ARGS, timeout=10)
' .
```

**After:**

```python
requests.get("https://api.example.com/status", timeout=10)
requests.get("https://api.example.com/data", timeout=5)
```

## Recipe 4: Unifying Renamed Functions (Order-Independent)

**Task:** Unify `send_json_payload(...)` and `post_data_as_json(...)` to `api_client.post(...)`,
regardless of keyword argument order. *(This requires `--inline-rules` to handle multiple conditions
(`any`, `all`) and order-insensitivity (`has`).)*

**Before:**

```python
send_json_payload(endpoint="/users", data={"name": "Alice"})
post_data_as_json(json_body={"name": "Bob"}, url="/products")
```

**Command:**

```bash
ast-grep -U --inline-rules '
id: unify-json-posting-functions-robust
language: python
rule:
  any:
    - all:
        - pattern: send_json_payload($$$_)
        - has: {pattern: endpoint = $URL}
        - has: {pattern: data = $PAYLOAD}
    - all:
        - pattern: post_data_as_json($$$_)
        - has: {pattern: url = $URL}
        - has: {pattern: json_body = $PAYLOAD}
  fix: api_client.post(url=$URL, json=$PAYLOAD)
' .
```

**After:**

```python
api_client.post(url="/users", json={"name": "Alice"})
api_client.post(url="/products", json={"name": "Bob"})
```

## Recipe 5: Modernizing `unittest` Assertions to `pytest`

**Task:** Convert `unittest` style assertions to modern `pytest` `assert` statements. *(Using
`--inline-rules` is best here to bundle multiple, related transformations into a single command.)*

**Before:**

```python
self.assertEqual(result, 4)
self.assertTrue(is_active)
self.assertIsNone(value)
```

**Command:**

```bash
ast-grep -U --inline-rules '
- id: refactor-assertEqual
  language: python
  rule: {pattern: self.assertEqual($A, $B), fix: "assert $A == $B"}
- id: refactor-assertTrue
  language: python
  rule: {pattern: self.assertTrue($A), fix: "assert $A"}
- id: refactor-assertIsNone
  language: python
  rule: {pattern: self.assertIsNone($A), fix: "assert $A is None"}
' .
```

**After:**

```python
assert result == 4
assert is_active
assert value is None
```

## When to Use What

### Simple Pattern/Replace: Use `-p` and `-r`

When you have a straightforward transformation with no special conditions:

```bash
ast-grep -U -p 'pattern' -r 'replacement' .
```

### Complex Rules: Use `--inline-rules`

When you need:

- Multiple patterns (`any`, `all`)
- Conditional logic (`not`, `has`)
- Multiple related transformations
- Order-independent matching

```bash
ast-grep -U --inline-rules '
id: my-transformation
language: python
rule:
  # Complex rule here
' .
```

## Common Patterns

### Metavariables

- `$VAR` - Matches a single node
- `$$$ARGS` - Matches zero or more nodes (ellipsis)
- `$_` - Anonymous metavariable (match but don't capture)

### Relational Operators

- `any: [...]` - Match if any rule matches
- `all: [...]` - Match if all rules match
- `not: {...}` - Match if rule doesn't match
- `has: {...}` - Match if contains pattern

## Code Conformance Rules

This project uses ast-grep for code conformance checking - enforcing banned patterns to maintain
code quality.

### How It Works

Code conformance rules are defined in `.ast-grep/rules/` and enforced at multiple points:

- **Pre-commit hooks**: Blocks commits with new violations
- **`poe lint`**: Includes conformance checking
- **Claude lint hook**: Shows violations after edits

### Example: Banning asyncio.sleep() in Tests

**Rule file** (`.ast-grep/rules/no-asyncio-sleep-in-tests.yml`):

```yaml
id: no-asyncio-sleep-in-tests
language: python
severity: error
message: "Avoid asyncio.sleep() in tests - use wait_for_condition() helper instead"
note: |
  Using asyncio.sleep() leads to flaky tests. Use wait_for_condition() instead.
rule:
  pattern: asyncio.sleep($$$)
```

**Checking for violations**:

```bash
# Check all files
scripts/check-conformance.sh

# Check specific files
scripts/check-conformance.sh tests/my_test.py
```

### Adding Exemptions

When you legitimately need a banned pattern, add an exemption:

**Inline exemption** (single line):

```python
# ast-grep-ignore: no-asyncio-sleep-in-tests - Mock simulates API delay
await asyncio.sleep(0.1)
```

**Block exemption** (multiple lines):

```python
# ast-grep-ignore-block: no-asyncio-sleep-in-tests - Mock implementation
async def mock_api_call():
    await asyncio.sleep(0.05)
    return data
# ast-grep-ignore-end
```

**File-level exemption** (`.ast-grep/exemptions.yml`):

```yaml
exemptions:
  - rule: no-asyncio-sleep-in-tests
    files:
      - tests/helpers.py
      - tests/mocks/*.py
    reason: "Test infrastructure legitimately needs sleep"
```

### Documentation

- [Code conformance rules](../../.ast-grep/rules/README.md) - Active rules and how to use them
- [Exemptions guide](../../.ast-grep/EXEMPTIONS.md) - How to add and manage exemptions

## Tips

1. **Test patterns first**: Use `ast-grep` without `-U` to see what matches
2. **Start simple**: Build complex rules incrementally
3. **Use YAML files**: For reusable rules, save to `.yml` files
4. **Check before applying**: Always review matches before using `-U` (update mode)
5. **Audit exemptions**: Run `scripts/audit-conformance-exemptions.sh` to review technical debt
