# Starlark Hermeticity: Theoretical vs Practical Concerns

## Is This a Real Problem?

### What Hermeticity Actually Means in Practice

**Theoretical Definition**: No I/O, no side effects, purely deterministic

**Practical Reality**: Starlark can't do I/O *directly*, but it can call functions you provide that do I/O

### How Others Actually Use Starlark

#### Example 1: Skycfg (Stripe's Config Management)
```go
// They expose functions with side effects
globals["http_get"] = starlark.NewBuiltin("http_get", httpGet)
globals["read_file"] = starlark.NewBuiltin("read_file", readFile)
```

#### Example 2: Starlight (Starlark in Go projects)
```go
// Common pattern - expose whatever functions you need
globals["log"] = StarLogger()        // Side effect
globals["fetch"] = HTTPFetcher()     // Network I/O
globals["save"] = DataSaver()        // Database writes
```

#### Example 3: Various Game Engines
- Expose functions to modify game state
- Call rendering functions
- Play sounds
- All "violating" hermeticity

### Do We Actually Need Hermetic Properties?

| Property | Do We Need It? | Why/Why Not |
|----------|----------------|-------------|
| **Determinism** | No | Event automation inherently depends on external state (time, sensors, etc.) |
| **Safe Parallelism** | No | We're not evaluating thousands of scripts simultaneously |
| **Reproducible Builds** | No | We're not building software |
| **Pure Functions** | No | The whole point is to have side effects |

### What We DO Need

1. **Sandboxing**: ✅ Starlark provides this even with side-effecting functions
2. **No Direct I/O**: ✅ Scripts can't access filesystem/network except through our APIs
3. **Resource Limits**: ✅ Can limit execution time and memory
4. **Safe Syntax**: ✅ No eval(), no arbitrary code execution

### Real vs Perceived Issues

#### "Breaking Hermeticity" - Not Really a Problem

```python
# This works fine in practice
def setup_starlark():
    # Scripts can't do I/O directly
    # ✅ No file access
    # ✅ No network access
    # ✅ No process execution
    
    # But they CAN call our functions that do I/O
    module["tools"] = {
        "turn_on_lights": lambda: api.lights_on(),     # We control this
        "send_notification": lambda m: api.notify(m),   # We control this
        "get_weather": lambda: api.weather(),           # We control this
    }
```

The sandbox is intact - scripts can only do what we explicitly allow.

#### "Loss of Determinism" - Acceptable Trade-off

```python
# Non-deterministic? Yes. Problem? No.
if time.hour >= sunset_hour():  # Changes daily
    if motion_detected():        # External state
        turn_on_lights()         # Side effect
```

Event automation is inherently non-deterministic. That's not a bug, it's the feature.

#### "No Parallel Execution" - Non-issue

We're not running thousands of scripts in parallel. Event processing is:
- Sequential (one event at a time)
- Fast enough (milliseconds per script)
- Not CPU-bound

### The Pragmatic View

```python
# What we want to write (natural, clear, LLM-friendly)
if event.temperature > 30:
    tools.send_notification("High temp: " + str(event.temperature))
    tools.turn_on_ac()

# What "proper" Starlark would require (unnatural)
if event.temperature > 30:
    return [
        {"action": "notify", "message": "High temp: " + str(event.temperature)},
        {"action": "turn_on_ac"}
    ]
# Then execute actions outside Starlark - why?
```

The second approach adds complexity for no practical benefit.

## Conclusion: It's a Theoretical Problem

1. **Starlark's hermeticity is about what the language can do directly, not what functions you expose**
2. **Many production users expose side-effecting functions to Starlark**
3. **We don't need the properties that true hermeticity provides**
4. **The sandboxing we need still works**

### Recommendation Stands: Use Starlark

- It's fine to expose side-effecting functions
- The "violation" of hermeticity is theoretical, not practical
- We still get the security benefits we need
- The alternative (two-phase execution) adds complexity for no real benefit

### But Document the Trade-offs

Be clear that our Starlark scripts:
- Are not deterministic (they depend on external state)
- Cannot be safely executed in parallel (due to side effects)
- Are not "pure" in the functional programming sense

These are acceptable trade-offs for event automation.