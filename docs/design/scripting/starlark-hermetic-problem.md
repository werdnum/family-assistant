# The Starlark Hermetic Execution Problem

## The Issue

Starlark's core design principles include:

- **Deterministic**: Same input always produces same output
- **Hermetic**: No I/O, no access to file system, network, or system clock
- **Parallel**: Safe for parallel evaluation

But we want to allow scripts to:

- Call tools that query databases (`search_notes`)
- Make network requests (`get_weather`)
- Control external systems (`turn_on_lights`)
- Send notifications (side effects)

This fundamentally violates hermeticity.

## How Others Handle This

### Bazel (Starlark's Origin)

- BUILD files use Starlark **hermeticly** to define the build graph
- Actual build actions (with side effects) execute **outside** Starlark
- Starlark describes WHAT to do, not HOW to do it

### Terraform

- HCL configuration is hermetic - it describes desired state
- The Terraform engine executes the actual changes
- Clear separation between configuration and execution

## Architectural Approaches

### Option 1: Pure Hermetic Starlark (Correct Pattern)

```python
# Starlark returns a plan, doesn't execute
def handle_event(event, context):
    actions = []
    
    if event.temperature > 30 and context.time.hour >= 6:
        # Don't execute, just plan
        actions.append({
            "type": "notification",
            "message": "High temperature: {}°C".format(event.temperature)
        })
        
        if context.state.get("lights_on"):
            actions.append({
                "type": "tool_call",
                "tool": "turn_off_lights",
                "args": {"room": "all"}
            })
    
    return {"actions": actions}
```

Then execute outside Starlark:
```python
# In Python, not Starlark
result = starlark_eval(script, context)
for action in result.get("actions", []):
    if action["type"] == "tool_call":
        await execute_tool(action["tool"], action["args"])
```

### Option 2: Non-Hermetic Functions (Pragmatic but "Wrong")

```python
# Register side-effecting functions
module["tools"] = {
    "turn_on_lights": lambda room: lights_api.turn_on(room),  # Side effect!
    "send_notification": lambda msg: notify_api.send(msg),    # Side effect!
}

# Starlark can now have side effects
if event.temperature > 30:
    tools.send_notification("High temp!")  # Breaks hermeticity
    tools.turn_on_ac()                     # Non-deterministic
```

### Option 3: Hybrid - Read-Only + Action List

```python
# Starlark gets read-only access and returns actions
def process_event(event, ctx):
    # Reading is "okay-ish" (still breaks pure hermeticity)
    recent_temps = ctx.db.get_recent("temperature", hours=1)
    avg_temp = sum(t.value for t in recent_temps) / len(recent_temps)
    
    # But writes must be returned as actions
    if event.temperature > avg_temp + 5:
        return {
            "condition_met": True,
            "actions": [
                {"tool": "alert", "args": {"message": "Temp spike!"}},
                {"tool": "log_metric", "args": {"metric": "temp_spike", "value": event.temperature}}
            ]
        }
    
    return {"condition_met": False, "actions": []}
```

## Why This Matters

### If We Break Hermeticity:

1. **Lost Determinism**: Same script might behave differently based on external state
2. **No Safe Parallelism**: Can't safely evaluate multiple scripts concurrently
3. **Harder Testing**: Need to mock all external dependencies
4. **Security Risks**: Scripts could probe the system through side effects
5. **Against Design Philosophy**: We're misusing the tool

### If We Keep Hermeticity:

1. **More Complex Architecture**: Need execution layer outside Starlark
2. **Less "Natural" Scripts**: Can't just `tools.turn_on_lights()`
3. **Two-Phase Execution**: Evaluate script → Execute actions
4. **More Boilerplate**: Every action needs to be wrapped

## Other Languages Reconsidered

### JavaScript (QuickJS, Duktape)

- Not designed to be hermetic
- Can naturally handle async I/O
- More suitable for side effects
- But harder to sandbox securely

### Lua

- Not inherently hermetic
- Commonly used for game scripting with side effects
- Can be sandboxed but not as strictly
- More natural for our use case?

### WebAssembly

- Can be hermetic OR allow imports
- Explicit capability-based security
- But complex integration

### Domain-Specific Language

- Design exactly what we need
- Can balance safety and functionality
- But high implementation cost

## Implications for Our Design

If we respect Starlark's hermetic principle:

```yaml
# Event listener returns action plan
listeners:
  - name: "Temperature Alert"
    condition:
      starlark: "event.temp > 30 and time.hour >= 6"
    plan:
      starlark: |
        # Pure computation - no side effects
        def create_plan(event, context):
            if event.temp > 35:
                severity = "high"
            else:
                severity = "medium"
                
            return {
                "execute": True,
                "actions": [{
                    "type": "notify",
                    "severity": severity,
                    "message": "Temperature: {}°C".format(event.temp)
                }]
            }
        
        create_plan(event, context)
```

The system would then:

1. Evaluate condition (hermetic)
2. Generate plan (hermetic)
3. Execute plan (side effects outside Starlark)

## The Uncomfortable Truth

Using Starlark for scripting with side effects is **philosophically wrong** according to its design principles. We have three choices:

1. **Respect the design**: Use Starlark properly with two-phase execution
2. **Violate the design**: Add side-effecting functions and lose guarantees
3. **Choose different tool**: Use something designed for imperative scripting

Each has significant trade-offs.