# Why Starlark-Only Makes More Sense Than CEL + Starlark

## The Flawed Premise

The original proposal suggested CEL for "simple expressions" and Starlark for "complex scripts." But this distinction is artificial when the primary author is an LLM.

## Problems with Two Languages

### 1. Double the Integration Code

With two languages, we need:

- Two parsers and evaluators
- Two sets of context bindings
- Two error handling systems
- Two sandboxing implementations
- Two sets of exposed APIs
- Two testing frameworks
- Two sets of documentation

This is a significant maintenance burden for marginal benefit.

### 2. Performance is Not a Real Concern

The performance argument (microseconds vs milliseconds) is largely irrelevant:

- Event listeners fire at most a few times per second, not thousands
- 1ms execution time is perfectly acceptable for automation rules
- The overhead of maintaining two systems outweighs microsecond savings
- Network I/O and tool execution dwarf script evaluation time

### 3. Starlark is Just as Simple for Simple Cases

CEL expression:

```text
event.temperature > 30 && time.hour >= 6 && time.hour <= 22

```

Starlark expression:

```python
event.temperature > 30 and time.hour >= 6 and time.hour <= 22

```

They're virtually identical! Starlark doesn't require functions or complex logic for simple conditions.

### 4. LLMs Don't Need Language Simplicity

- LLMs can write Starlark as easily as CEL
- They don't benefit from "simpler" syntax
- They don't need IDE support or syntax highlighting
- One consistent language is actually simpler for prompt engineering

### 5. Real Systems Use One Language

Looking at successful automation systems:

- **Bazel**: Uses Starlark for everything from simple globs to complex build logic
- **Terraform**: HCL handles both simple variable references and complex provisioning logic
- **GitHub Actions**: One expression syntax that scales from simple to complex
- **Ansible**: Jinja2 for both simple variables and complex templating
- **Home Assistant**: Jinja2 templates scale from simple to complex

I can't find examples of successful systems using two different languages for "simple" vs "complex" automation.

## Benefits of Starlark-Only

### 1. Unified Mental Model

```python

# Simple condition (what we thought needed CEL)
event.state > 30 and time.hour >= 6

# Slightly more complex (still one line)
event.state > 30 and time.hour >= 6 and not state.alerted_today

# Complex logic (natural progression)
def should_alert():
    if event.state <= 30:
        return False
    if time.hour < 6 or time.hour > 22:
        return False
    if state.alerted_today:
        return False
    # Check recent history
    recent = [e for e in db.get_recent_events("temp", 1) if e.value > 30]
    return len(recent) < 3  # Only alert if not consistently hot

should_alert()

```

The progression from simple to complex is natural, no language switch needed.

### 2. Single Integration Point

```python
class StarlarkEngine:
    def evaluate_condition(self, expr: str, context: dict) -> bool:
        """Evaluate a boolean expression"""
        return self.execute(expr, context)

    def execute_action(self, script: str, context: dict) -> Any:
        """Execute an action script"""
        return self.execute(script, context)

    def execute(self, code: str, context: dict) -> Any:
        """Single execution method for all cases"""
        # One parser, one evaluator, one context binding

```

### 3. Better for LLM Prompting

Instead of:

```yaml
system_prompt: |
  For simple conditions, use CEL syntax (C-style expressions).
  For complex logic, use Starlark (Python-like syntax).
  CEL examples: event.value > 30, time.hour >= 6
  Starlark examples: def process(): ...

```

We have:

```yaml
system_prompt: |
  Use Starlark (Python-like syntax) for all automation logic.
  Simple: event.value > 30 and time.hour >= 6
  Complex: def process(): ...

```

### 4. Natural Feature Growth

Starting simple:

```python

# Version 1: Basic temperature alert
event.temperature > 30

```

Growing organically:

```python

# Version 2: Add time restriction (no language change!)
event.temperature > 30 and time.hour >= 6 and time.hour <= 22

```

More complex:

```python

# Version 3: Add rate limiting (still same language!)
temp = event.temperature
if temp > 30 and time.is_between(6, 22):
    last_alert = state.get("last_temp_alert", 0)
    if time.since(last_alert) > 3600:  # 1 hour
        state.set("last_temp_alert", time.now)
        True
    else:
        False
else:
    False

```

## Implementation Simplification

### Before (Two Languages)

```python

# In event processor
if listener.get("cel_condition"):
    if not evaluate_cel(listener["cel_condition"], context):
        return

if listener.get("action_type") == "script":
    execute_starlark(listener["script"], context)

```

### After (Starlark Only)

```python

# In event processor
if listener.get("condition"):
    if not starlark_eval(listener["condition"], context):
        return

if listener.get("action"):
    starlark_exec(listener["action"], context)

```

## Security is Equivalent

Both CEL and Starlark:

- Are sandboxed (no I/O without explicit APIs)
- Have resource limits
- Are deterministic
- Prevent infinite loops (with execution timeouts)

The "non-Turing complete" advantage of CEL is theoretical - in practice, Starlark with timeouts is equally safe.

## Migration Path is Cleaner

With Starlark-only:

1. Start with simple expressions in event conditions
2. Gradually add more complex logic as needed
3. No rewriting when requirements grow
4. No decision paralysis about which language to use

## Conclusion

Using Starlark for everything is:

- **Simpler**: One language, one integration, one mental model
- **More maintainable**: Half the code to maintain
- **More flexible**: Natural progression from simple to complex
- **Better for LLMs**: Consistent syntax, no language selection decision
- **Industry-proven**: Follows patterns of successful automation systems

The marginal benefits of CEL (slightly faster, non-Turing complete) don't justify the complexity of maintaining two language integrations. For an LLM-first system, a single expressive language is the better choice.
