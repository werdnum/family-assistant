# Final Scripting Language Recommendation

## Executive Summary

After thorough analysis including the hermiticity concern, we recommend **Starlark (via starlark-pyo3)** as the single scripting language for Family Assistant.

## Key Insights

1. **Hermiticity is a theoretical concern, not a practical problem**
   - Starlark prevents direct I/O (good for security)
   - But allowing side effects through provided functions is normal and widely done
   - We don't need determinism or parallel execution for event automation

2. **Single language is simpler**
   - One parser, one sandbox, one set of APIs
   - Natural progression from simple to complex
   - No language selection decision for the LLM

3. **Starlark + side effects is a common pattern**
   - Many production systems expose I/O functions to Starlark
   - The sandboxing still works - scripts can only call what we provide
   - This is how embedded scripting typically works

## What Our Implementation Looks Like

```python
import starlark as sl

# Create module with our APIs - yes, they have side effects!
module = sl.Module()
module["event"] = event_data
module["time"] = TimeAPI()
module["state"] = StateAPI()
module["tools"] = {
    "send_notification": lambda msg: notify_api.send(msg),  # Side effect!
    "turn_on_lights": lambda room: lights_api.on(room),    # Side effect!
    "get_weather": lambda: weather_api.current(),          # External I/O!
}

# Scripts can now do useful automation
script = """
if event.temperature > 30 and time.hour >= 6:
    tools.send_notification("High temp: " + str(event.temperature))
    if not state.get("ac_on"):
        tools.turn_on_ac()
        state.set("ac_on", True)
"""

# Execute - it works fine!
ast = sl.parse("automation", script)
result = sl.eval(module, ast, globals)
```

## Why This Works

1. **Security**: Scripts can't access filesystem, network, or system directly
2. **Control**: We explicitly choose what functions to expose
3. **Simplicity**: Direct function calls are natural for automation
4. **LLM-friendly**: Straightforward Python-like syntax

## What We're Trading Off

- **Not deterministic**: Scripts depend on external state (time, sensors)
- **Not parallel-safe**: Side effects mean no concurrent execution
- **Not "pure"**: We're doing imperative scripting, not functional programming

**These trade-offs are fine for event automation.**

## Implementation Plan

1. **Use starlark-pyo3** - Rust-based, fast, well-maintained
2. **Expose necessary APIs** - Tools, state, database queries
3. **Single language everywhere** - Conditions and actions both use Starlark
4. **Clear documentation** - Be upfront about side effects

## Example Configuration

```yaml
event_listeners:
  - name: "Temperature Control"
    # Simple condition - just an expression
    condition: "event.temperature > 30 and time.hour >= 6"
    
    # Complex action - full script with side effects
    action: |
      # This is fine! We want side effects!
      tools.send_notification("Temperature: " + str(event.temperature))
      
      if event.temperature > 35:
          tools.turn_on_ac()
          tools.set_fan("high")
      else:
          tools.set_fan("medium")
      
      state.set("last_temp_alert", time.now)
```

## Bottom Line

Starlark with side-effecting functions is:

- **Practically sound** - Widely used pattern
- **Theoretically impure** - But we don't need purity
- **The right tool** - Simpler than alternatives

The hermiticity principle is about what Starlark can do directly, not what we choose to expose to it. Our approach is pragmatic, secure, and aligned with how embedded scripting languages are actually used in production.