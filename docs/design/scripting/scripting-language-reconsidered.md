# Reconsidering Scripting Language Choice

## The Hermetic Problem Changes Everything

After realizing that Starlark's hermetic design principle conflicts with our need for side effects, we need to reconsider our options.

## What We Actually Need

1. **Side Effects**: Scripts must be able to call tools, query databases, send notifications
2. **Conditional Logic**: If/then/else, loops, functions
3. **Safe Execution**: Sandboxed from filesystem/network unless through our APIs
4. **LLM-Friendly**: Syntax an LLM can easily generate
5. **Deterministic-ish**: Predictable behavior (but true determinism impossible with external I/O)

## Re-evaluating Options

### Option 1: Lua (Reconsidered) ⭐ New Top Choice

```text
-- Natural for automation with side effects
if event.temperature > 30 and time.hour >= 6 then
    tools.send_notification("High temperature: " .. event.temperature .. "°C")
    if state.get("ac_on") == false then
        tools.turn_on_ac()
        state.set("ac_on", true)
    end
end

```

**Pros:**

- **Designed for embedding**with controlled side effects
- **Widely used**in game engines for exactly this pattern
- **Simple syntax**that LLMs handle well
- **Natural sandboxing**- only expose what you want
- **Proven pattern**- Redis, Nginx, games all use Lua this way

**Cons:**

- Not Python-like syntax
- Another language for LLM to learn (but it's simple)

### Option 2: JavaScript (QuickJS) ⭐ Worth Considering

```javascript
if (event.temperature > 30 && time.hour >= 6) {
    await tools.sendNotification(`High temperature: ${event.temperature}°C`);
    if (!state.get("ac_on")) {
        await tools.turnOnAC();
        state.set("ac_on", true);
    }
}

```

**Pros:**

- **Async-native**- designed for I/O operations
- **Ubiquitous**- most known language
- **QuickJS**is small and embeddable
- **Natural for side effects**

**Cons:**

- Async complexity
- Harder to sandbox perfectly
- JavaScript quirks

### Option 3: Starlark with Proper Architecture

Use Starlark as designed - scripts return action plans:

```python

# Starlark script - pure logic, no side effects
def process_event(event, context):
    actions = []

    if event.temperature > 30 and context.time.hour >= 6:
        actions.append({
            "action": "notify",
            "message": "High temperature: {}°C".format(event.temperature)
        })

        if not context.state.get("ac_on"):
            actions.append({"action": "turn_on_ac"})
            actions.append({
                "action": "set_state",
                "key": "ac_on",
                "value": True
            })

    return actions

```

**Pros:**

- **Philosophically correct**- respects Starlark's design
- **Purely deterministic**planning phase
- **Safe parallelism**for evaluation
- **Clear separation**of logic and effects

**Cons:**

- **Unnatural for LLM**- must return data structures, not execute
- **More complex**- two-phase execution
- **Verbose**- can't just call functions

### Option 4: Minimal DSL

Design exactly what we need:

```text
WHEN event.temperature > 30 AND time.hour BETWEEN 6 AND 22:
    NOTIFY "High temperature: {event.temperature}°C"
    IF NOT state.ac_on:
        CALL turn_on_ac
        SET state.ac_on = true

```

**Pros:**

- **Purpose-built**for our use case
- **No impedance mismatch**
- **Easiest to secure**

**Cons:**

- **High implementation effort**
- **Yet another language**for LLM
- **Limited ecosystem**

## Recommendation Revised

### For Simple Conditions: Still Starlark (Hermetic)

```python

# Pure boolean expressions are fine
event.temperature > 30 and time.hour >= 6 and not state.get("alerted_today")

```

### For Automation Scripts: Lua

**Why Lua:**

1. **Designed for this**- Embedding with controlled side effects is Lua's sweet spot
2. **Proven pattern**- Games, Redis, Nginx all use Lua exactly this way
3. **Simple enough**- LLMs can learn it easily
4. **Natural control**- We expose exactly the functions we want
5. **Lupa is mature**- Good Python integration

**Implementation approach:**

```python

# Python side
import lupa

lua = lupa.LuaRuntime(unpack_returned_tuples=True)

# Expose our APIs
lua.globals().event = event_data
lua.globals().time = time_api
lua.globals().tools = safe_tools_api
lua.globals().state = state_api

# Execute
result = lua.execute(script_code)

```

### Architecture

```text
┌─────────────────────────┐
│   Event Listener        │
├─────────────────────────┤
│ Condition (Starlark)    │ ← Pure, hermetic, deterministic
├─────────────────────────┤
│ Action Script (Lua)     │ ← Side effects, tool calls
└─────────────────────────┘

```

Or simpler:

```text
┌─────────────────────────┐
│   Event Listener        │
├─────────────────────────┤
│ Lua Script              │ ← Both condition and action
│ (returns true/false     │
│  and executes actions)  │
└─────────────────────────┘

```

## Why Not Starlark for Everything?

Because using Starlark with side effects is:

1. **Against its design philosophy**- It's meant to be hermetic
2. **Losing its benefits**- No more determinism or safe parallelism
3. **Making it worse Python**- If we want side effects, use something designed for it
4. **Creating impedance mismatch**- Fighting the tool's nature

## Final Recommendation

**Use Lua via lupa for all scripting needs:**

- Simple conditions work fine: `return event.temperature > 30 and time.hour >= 6`
- Complex automations are natural: Direct tool calls and state management
- One language, designed for embedding with side effects
- Proven pattern in production systems
- Good Python integration

The Starlark hermeticity issue is a fundamental design mismatch. Rather than fight it, choose a tool designed for what we need: embedded scripting with controlled side effects.
