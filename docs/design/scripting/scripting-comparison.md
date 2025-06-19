# Scripting Approach Comparison: Starlark-Only vs CEL+Starlark

## Quick Comparison

| Aspect | Starlark-Only | CEL + Starlark |
|--------|---------------|----------------|
| **Languages to maintain**| 1 | 2 |
| **Integration code**| ~500 lines | ~1000 lines |
| **Dependencies**| starlark-pyo3 | cel-python + starlark-pyo3 |
| **Simple expression syntax**| `event.temp > 30 and time.hour > 6` | `event.temp > 30 && time.hour > 6` |
| **LLM complexity**| One language to learn | Two languages, must choose which |
| **Migration path**| Expressions grow into scripts naturally | Must rewrite when moving from CEL to Starlark |
| **Testing burden**| Test one system | Test two systems + interaction |
| **Security surface**| One sandbox to secure | Two sandboxes to secure |
| **Documentation**| One language to document | Two languages + when to use which |

## Code Comparison

### Event Listener Configuration

**Starlark-Only Approach:**

```yaml
listeners:

  - name: "Temperature Alert"
    filter: "event.temp > 30 and time.hour >= 6 and time.hour <= 22"
    action:
      type: notification

  - name: "Complex Temperature Alert"
    filter: |
      # Still Starlark, just more complex
      if event.temp > 30 and time.is_between(6, 22):
          recent = [e for e in db.get_recent_events("temp", 1) if e.value > 30]
          len(recent) < 3  # Only alert if not consistently hot
      else:
          False

```

**CEL + Starlark Approach:**

```yaml
listeners:

  - name: "Temperature Alert"
    cel_filter: "event.temp > 30 && time.hour >= 6 && time.hour <= 22"
    action:
      type: notification

  - name: "Complex Temperature Alert"
    # Can't do this in CEL, must switch to Starlark
    action:
      type: script
      language: starlark  # Need to specify language
      code: |
        if event.temp > 30 and time.is_between(6, 22):
            recent = [e for e in db.get_recent_events("temp", 1) if e.value > 30]
            return len(recent) < 3
        else:
            return False

```

### Implementation Code

**Starlark-Only:**

```python
class ScriptEngine:
    def evaluate(self, code: str, context: dict) -> Any:
        """One method handles all cases"""
        module = self.create_module(context)
        ast = sl.parse("script", code)
        return sl.eval(module, ast, self.globals)

# In event processor
if listener.get("filter"):
    if not engine.evaluate(listener["filter"], context):
        return

```

**CEL + Starlark:**

```python
class ScriptEngine:
    def __init__(self):
        self.cel_env = cel.Environment()
        self.starlark_globals = sl.Globals.standard()

    def evaluate_cel(self, expr: str, context: dict) -> Any:
        """CEL evaluation"""
        ast = self.cel_env.compile(expr)
        program = self.cel_env.program(ast)
        return program.evaluate(context)

    def evaluate_starlark(self, code: str, context: dict) -> Any:
        """Starlark evaluation"""
        module = self.create_module(context)
        ast = sl.parse("script", code)
        return sl.eval(module, ast, self.starlark_globals)

# In event processor - more complex
if listener.get("cel_filter"):
    if not engine.evaluate_cel(listener["cel_filter"], context):
        return
elif listener.get("starlark_filter"):
    if not engine.evaluate_starlark(listener["starlark_filter"], context):
        return

```

## LLM Prompt Comparison

**Starlark-Only:**

```yaml
system_prompt: |
  Generate Starlark code for automation rules.

  Simple conditions are just expressions:

  - event.temperature > 30
  - time.hour >= 6 and time.hour <= 22
  - state.get("alerted") != True

  Complex logic uses functions and control flow:

  - if/else statements
  - for loops over db.get_recent_events()
  - def functions for reusable logic

```

**CEL + Starlark:**

```yaml
system_prompt: |
  Generate automation rules using the appropriate language:

  For simple boolean conditions, use CEL:

  - event.temperature > 30
  - time.hour >= 6 && time.hour <= 22
  - !state.alerted

  For complex logic requiring loops, functions, or multiple statements, use Starlark:

  - Processing lists of events
  - Multi-step decision trees
  - Stateful operations

  IMPORTANT: Choose the right language for each use case.

```

## Real-World Evolution Example

**Starlark-Only - Natural progression:**

```python

# Version 1: Simple
event.motion == "detected"

# Version 2: Add time check (no rewrite!)
event.motion == "detected" and time.hour >= 20

# Version 3: Add occupancy (still no rewrite!)
event.motion == "detected" and time.hour >= 20 and state.get("home") == True

# Version 4: Add logic (natural extension!)
if event.motion == "detected" and state.get("home") == True:
    if time.hour >= 20 or time.hour < 6:
        # Check if we already turned on lights recently
        last_activation = state.get("lights_activated", 0)
        if time.since(last_activation) > 300:  # 5 minutes
            True
        else:
            False
    else:
        False
else:
    False

```

**CEL + Starlark - Requires language switch:**

```javascript
// Version 1-3: CEL works fine
event.motion == "detected" && time.hour >= 20 && state.home == true

// Version 4: Oops, need state checking and complex logic
// MUST REWRITE IN DIFFERENT LANGUAGE:

```

```python

# Start over in Starlark

if event.motion == "detected" and state.get("home") == True:
    # ... rest of logic

```

## Maintenance Burden

**Starlark-Only:**

- Update starlark-pyo3 when new versions release
- Maintain one set of context bindings
- One security audit surface
- One set of tests

**CEL + Starlark:**

- Update cel-python AND starlark-pyo3
- Maintain two sets of context bindings
- Ensure consistent behavior between languages
- Security audit both implementations
- Test both systems AND their interaction
- Document when to use which language

## Conclusion

The Starlark-only approach is:

- **Simpler**: Half the code, one mental model
- **More flexible**: Natural progression from simple to complex
- **LLM-friendly**: No language selection decision
- **Maintainable**: One system to update and secure
- **User-friendly**: Even human users only need to learn one language

The marginal performance benefit of CEL (microseconds vs milliseconds) doesn't justify doubling the system complexity.
