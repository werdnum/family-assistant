# Scripting Language Integration Proposal for Family Assistant (Revised)

## Executive Summary

This proposal outlines the integration of Starlark as the single scripting language for Family Assistant. After careful analysis, we recommend using **Starlark for all scripting needs**- from simple event conditions to complex automations. This approach reduces complexity while providing a secure, performant, and LLM-friendly scripting environment.

## Why Scripting?

### Primary Use Cases

1. **Event Automation**
   - Simple conditions: `event.temperature > 30 and time.hour >= 6`
   - Complex workflows with branching logic and tool orchestration
   - Rate limiting and state management

2. **Security Enhancement**
   - Pre-planned action sequences before processing untrusted input
   - Deterministic execution with predictable outcomes
   - Comprehensive audit trails

3. **Cost Reduction**
   - Eliminate LLM calls for mechanical tasks
   - Cache computed results
   - Reduce token usage for repetitive operations

## Why Starlark Only?

### Single Language Benefits

1. **Simplicity**
   - One parser, one evaluator, one set of APIs
   - Half the integration code to maintain
   - Single mental model for all automation

2. **Natural Progression**
   - Simple expressions work just like complex scripts
   - No rewriting when requirements grow
   - Consistent syntax throughout

3. **LLM-Optimized**
   - LLMs write Starlark as easily as any expression language
   - No language selection decision needed
   - Simpler prompts and consistent examples

4. **Industry-Proven**
   - Bazel uses Starlark for all configuration complexity levels
   - Follows successful automation system patterns
   - Well-tested in production environments

### Why Not CEL + Starlark?

The original proposal suggested CEL for simple expressions, but:

- Performance difference (microseconds vs milliseconds) is negligible for event automation
- Starlark is equally simple for basic expressions
- Two languages mean double the maintenance burden
- No successful automation systems use this dual-language pattern

## Starlark Implementation

### Python Binding: starlark-pyo3

We'll use `starlark-pyo3` for the following reasons:

- **Simple Installation**: Binary wheels, no build dependencies
- **Active Maintenance**: 35K+ weekly PyPI downloads
- **Security**: Rust-based implementation with strong sandboxing
- **Performance**: No Python GIL restrictions
- **Documentation**: Comprehensive API documentation

### Installation

```yaml
[project]
dependencies = [
    # ... existing dependencies
    "starlark-pyo3>=0.1.0",  # Rust-based Starlark implementation
]

```

## API Design

### Script Context

```python

# All scripts receive a context object with these APIs

context = {
    # Event data (for event-triggered scripts)
    "event": {
        "type": str,
        "source": str,
        "data": dict,
        "metadata": dict
    },

    # Time utilities
    "time": {
        "now": timestamp,
        "hour": int,
        "day_of_week": int,
        "is_between(start_hour, end_hour)": bool,
        "since(timestamp)": seconds
    },

    # Read-only database access
    "db": {
        "get_notes(query, limit=10)": list,
        "get_recent_events(type, hours=24)": list,
        "get_user_data(key)": any
    },

    # State management
    "state": {
        "get(key, default=None)": any,
        "set(key, value)": None,
        "delete(key)": None
    },

    # Tool execution (when enabled)
    "tools": {
        "execute(name, **args)": result,
        "is_available(name)": bool
    }
}

```

## Usage Examples

### Simple Event Condition

```yaml
event_listeners:

  - name: "High Temperature Alert"
    conditions:

      - source: home_assistant
        entity_id: sensor.outside_temperature
    # Simple one-line Starlark expression
    filter: "event.data.state > 30 and time.hour >= 6 and time.hour <= 22"
    action:
      type: notification
      message: "High temperature: {{event.data.state}}°C"

```

### Progressive Complexity

```python

# Start simple - just a boolean expression
event.temperature > 30

# Add time restriction (same language!)
event.temperature > 30 and time.hour >= 6 and time.hour <= 22

# Add state checking (still same language!)
event.temperature > 30 and time.hour >= 6 and time.hour <= 22 and not state.get("alerted_today", False)

# Grow to multi-line logic (natural progression!)
temp = event.temperature
daytime = time.hour >= 6 and time.hour <= 22
already_alerted = state.get("alerted_today", False)

if temp > 30 and daytime and not already_alerted:
    state.set("alerted_today", True)
    True
else:
    False

# Eventually add functions (when needed)
def should_alert():
    if event.temperature <= 30:
        return False
    if not time.is_between(6, 22):
        return False
    if state.get("alert_count_today", 0) >= 3:
        return False

    # Check if temperature is rising
    recent = db.get_recent_events("temperature", hours=1)
    if len(recent) >= 2:
        trend = recent[-1].value - recent[0].value
        if trend < 2:  # Only alert if rising quickly
            return False

    state.set("alert_count_today", state.get("alert_count_today", 0) + 1)
    return True

should_alert()

```

### Complex Automation Script

```yaml
event_listeners:

  - name: "Smart Motion Lights"
    conditions:

      - source: home_assistant
        entity_id: binary_sensor.motion
    action:
      type: script
      allow_tools: true
      code: |
        # Multi-step automation with tools
        def handle_motion():
            room = event.metadata.get("room", "unknown")
            motion = event.data.state == "on"

            if not motion:
                return {"action": "none", "reason": "no motion"}

            # Check context
            lights = tools.execute("get_lights", room=room)
            luminosity = tools.execute("get_sensor", entity="sensor.{}_luminosity".format(room))

            # Decide based on conditions
            if time.hour >= 22 or time.hour < 6:
                # Late night - minimal lighting
                if not lights.any_on:
                    tools.execute("turn_on_lights", room=room, brightness=10, color_temp=2000)
                    state.set("auto_lights_{}".format(room), time.now)
                    return {"action": "dim_lights", "brightness": 10}

            elif luminosity and luminosity.value < 100:
                # Dark enough to need lights
                brightness = 100 if time.hour >= 8 and time.hour <= 20 else 60
                tools.execute("turn_on_lights", room=room, brightness=brightness)
                state.set("auto_lights_{}".format(room), time.now)
                return {"action": "normal_lights", "brightness": brightness}

            return {"action": "none", "reason": "bright enough"}

        handle_motion()

```

## Implementation Architecture

### Single Script Engine

```python

# src/family_assistant/scripting/engine.py
import starlark as sl
from typing import Any, Dict, Optional

class StarlarkEngine:
    """Unified Starlark execution engine"""

    def __init__(self, db_context):
        self.db_context = db_context
        self._globals = sl.Globals.standard()

    def evaluate(self, expression: str, context: Dict[str, Any]) -> Any:
        """Evaluate any Starlark code - simple or complex"""
        module = self._create_module(context)
        ast = sl.parse("script", expression)
        return sl.eval(module, ast, self._globals)

    def _create_module(self, context: Dict[str, Any]) -> sl.Module:
        """Create Starlark module with context"""
        module = sl.Module()

        # Add all context APIs
        module["event"] = context.get("event", {})
        module["time"] = TimeAPI()
        module["db"] = DatabaseAPI(self.db_context)
        module["state"] = StateAPI()

        if context.get("allow_tools"):
            module["tools"] = ToolsAPI(context["tools_provider"])

        return module

```

### Event Processor Integration

```python

# Simplified - one language for all cases
async def process_event_listener(listener, event):
    # Check filter condition (if present)
    if "filter" in listener:
        if not engine.evaluate(listener["filter"], {"event": event}):
            return

    # Execute action
    action = listener.get("action", {})
    if action.get("type") == "script":
        result = engine.evaluate(
            action["code"],
            {
                "event": event,
                "allow_tools": action.get("allow_tools", False),
                "tools_provider": self.tools_provider
            }
        )
        logger.info(f"Script result: {result}")

```

## Security Model

### Sandboxing

Starlark provides strong sandboxing:

- No file system access
- No network access
- No process execution
- No access to Python internals
- Deterministic execution

### Resource Limits

```yaml
scripting:
  enabled: true
  starlark:
    max_execution_time_ms: 5000
    max_memory_mb: 50
    max_script_size: 50000
  security:
    allowed_tools:

      - get_weather
      - send_notification
      - get_lights
      - turn_on_lights
    rate_limits:
      executions_per_minute: 30
      tool_calls_per_execution: 10

```

### Audit Logging

All script executions are logged with:

- Script content hash
- Execution context
- Result/error
- Resource usage
- Tool calls made

## Migration Path

### Phase 1: Event Conditions (Week 1-2)

1. Add Starlark engine with basic context
2. Support simple expressions in event filters
3. Test with existing event listeners
4. Add execution logging

### Phase 2: Action Scripts (Week 3-4)

1. Enable script actions with tool access
2. Implement resource limits and timeouts
3. Add state management API
4. Create initial examples

### Phase 3: Advanced Features (Week 5-6)

1. Script storage and versioning
2. Web UI for script editing
3. Script templates library
4. Performance monitoring

### Phase 4: Extended Use Cases (Week 7-8)

1. Custom tool definitions
2. Scheduled scripts
3. Script composition/importing
4. Advanced debugging tools

## Benefits Over Dual-Language Approach

1. **50% Less Code**: One integration instead of two
2. **Simpler Mental Model**: One language for all cases
3. **Better for LLMs**: No language selection decision
4. **Natural Growth**: Scripts evolve without rewrites
5. **Proven Pattern**: Follows successful automation systems

## Performance Considerations

- Simple expressions: ~1ms (acceptable for event filtering)
- Complex scripts: 5-50ms (fine for automation tasks)
- Tool calls dominate execution time, not script evaluation
- Can process hundreds of events per second if needed

## Success Metrics

1. **Adoption**: Number of event listeners using scripts vs LLM
2. **Cost Savings**: LLM tokens saved per month
3. **Reliability**: Script success rate vs LLM success rate
4. **Performance**: 95th percentile execution time < 100ms
5. **Security**: Zero sandbox escapes or security incidents

## Conclusion

Using Starlark as the single scripting language for Family Assistant provides a simpler, more maintainable, and equally powerful solution compared to a dual-language approach. The Rust-based starlark-pyo3 implementation offers excellent security and performance characteristics while maintaining the simplicity needed for LLM-generated scripts. This unified approach follows industry best practices and provides a solid foundation for extensible automation.
