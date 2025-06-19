# Scripting Language Integration Proposal for Family Assistant

> **Note**: This is the original proposal that recommended CEL + Starlark. After further analysis, we now recommend a **Starlark-only approach**. See [scripting-language-proposal-v2.md](./scripting-language-proposal-v2.md) for the updated recommendation.

## Executive Summary

This proposal outlines options for integrating a restricted scripting language into Family Assistant to enable user customization, reduce LLM costs, and improve security. Based on extensive research, we recommend **CEL (Common Expression Language)**for simple expressions and **Starlark**for more complex scripting needs, with a phased implementation starting with event listener actions.

## Use Case Analysis

### Primary Use Cases

1. **Event Listener Actions**
   - Complex conditions based on time, entity states, or tool results
   - Custom message formatting without LLM calls
   - Multi-step automations with branching logic

2. **Security Enhancement**
   - Pre-planned action sequences before untrusted input exposure
   - Predictable execution patterns
   - Audit trail of scripted actions

3. **Cost Reduction**
   - Mechanical tasks executed as code instead of LLM calls
   - Reduced token usage for repetitive operations
   - Cached script results

## Language Comparison Matrix

| Language | Security | Performance | Python Integration | Learning Curve | Use Case Fit |
|----------|----------|-------------|-------------------|----------------|--------------|
| **CEL**| ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | Event conditions, simple rules |
| **Starlark**| ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | Complex automation, workflows |
| **Lua (lupa)**| ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | General scripting |
| **RestrictedPython**| ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | Python compatibility |
| **JavaScript (pyduktape)**| ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | Web developers |
| **WebAssembly**| ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐ | ⭐⭐ | Maximum isolation |

### Detailed Analysis

#### CEL (Common Expression Language)

- **Pros**: Non-Turing complete, designed for security, nanosecond evaluation, Google-backed
- **Cons**: Limited to expressions, no loops or complex logic
- **Best for**: Event conditions, validation rules, simple transformations

#### Starlark

- **Pros**: Python-like syntax, deterministic execution, designed for configuration + logic
- **Cons**: Requires external bindings (Rust or Go implementation)
- **Best for**: Complex automations, multi-step workflows, tool orchestration
- **Recommended Implementation**: `starlark-pyo3` (Rust-based with PyO3 bindings)

#### Lua

- **Pros**: Mature embedding story, good performance, smaller attack surface than Python
- **Cons**: Different syntax from Python, sandboxing requires care with lupa
- **Best for**: General-purpose scripting if Python syntax not required

## Starlark Implementation Details

### Python Bindings Comparison

We evaluated two main options for Starlark integration:

1. **starlark-pyo3**(Recommended)
   - Built on Facebook's Rust implementation with PyO3 bindings
   - Simple installation via binary wheels (no build dependencies)
   - Active maintenance (35K+ weekly PyPI downloads)
   - Comprehensive documentation
   - Clean 2-layer architecture (Python → Rust)
   - Uses JSON for value conversion (sufficient for our use cases)

2. **python-starlark-go**
   - Built on Google's Go implementation with CGO
   - Requires C compiler for installation
   - Based on the reference implementation used in Bazel
   - More complex 3-layer architecture (Python → C → Go)
   - Less active Python binding maintenance

### Why starlark-pyo3

The Rust-based implementation provides:

- **Security**: True sandboxing with no filesystem, network, or system access
- **Performance**: Excellent performance without Python GIL restrictions
- **Ease of Use**: Simple pip install, no build dependencies
- **Reliability**: Strong type system and Rust's memory safety
- **Documentation**: Well-documented API with clear examples

## Recommended Solution: Hybrid Approach

### Phase 1: CEL for Event Conditions

Use CEL for event listener conditions and simple rules:

```text
// Example: Only notify if temperature > 30 and during daytime
event.temperature > 30 &&
time.hour >= 6 && time.hour <= 22 &&
!has(state.notifications_sent_today)

```

### Phase 2: Starlark for Complex Scripts

Use Starlark for multi-step automations and workflows:

```python

# Starlark script for complex automation
def handle_event(event, context):
    if event.type == "motion_detected":
        lights = context.tools.get_lights(event.room)
        if context.time.is_night() and not lights.any_on():
            context.tools.turn_on_lights(event.room, brightness=30)
            context.tools.schedule_task(
                "turn_off_lights",
                room=event.room,
                delay_minutes=10
            )

```

## API Design

### Core Script Context API

```python
@dataclass
class ScriptContext:
    """Base context provided to all scripts"""
    event: EventData           # Current event data
    time: TimeAPI             # Time utilities
    db: DatabaseReadAPI       # Read-only database access
    state: StateAPI          # System state access

@dataclass
class AutomationContext(ScriptContext):
    """Extended context for automation scripts"""
    tools: SafeToolsAPI      # Tool execution
    notify: NotificationAPI  # Send notifications
    schedule: SchedulerAPI   # Schedule tasks

```

### Safe Database API

```python
class DatabaseReadAPI:
    """Read-only database access for scripts"""

    async def get_notes(self,
                       query: Optional[str] = None,
                       limit: int = 10) -> List[Note]:
        """Search notes with optional query"""

    async def get_recent_events(self,
                               event_type: Optional[str] = None,
                               hours: int = 24) -> List[Event]:
        """Get recent events, optionally filtered by type"""

    async def get_user_data(self, key: str) -> Optional[Any]:
        """Get user-defined data by key"""

```

### Safe Tools API

```python
class SafeToolsAPI:
    """Controlled tool execution for scripts"""

    async def execute(self,
                     tool_name: str,
                     **kwargs) -> ToolResult:
        """Execute a whitelisted tool with arguments"""
        # Validates tool is allowed for scripts
        # Applies rate limiting
        # Logs execution for audit

    def is_available(self, tool_name: str) -> bool:
        """Check if tool is available for script use"""

```

## Implementation Plan

### Stage 1: Foundation (Week 1-2)

1. Set up CEL-python for expression evaluation
2. Install starlark-pyo3 (`pip install starlark-pyo3`)
3. Create base ScriptContext and API classes
4. Implement security sandbox for script execution
5. Add configuration for enabling/disabling scripting

### Stage 2: Event Integration (Week 3-4)

1. Add `script` action type to event listeners
2. Implement CEL condition evaluation for events
3. Create script execution environment with context
4. Add logging and error handling

### Stage 3: Starlark Integration (Week 5-6)

1. Integrate starlark-pyo3 for complex scripts
2. Implement script storage and management
3. Create script editor UI (web interface)
4. Add script validation and testing tools
5. Create comprehensive API documentation

### Stage 4: Advanced Features (Week 7-8)

1. Custom tool creation via scripts
2. Scheduled script execution
3. Script sharing and templates
4. Performance monitoring and optimization

## Security Architecture

### Multi-Layer Security Model

1. **Language Level**
   - CEL: Non-Turing complete, no infinite loops
   - Starlark: No I/O, deterministic execution
   - Type-safe APIs prevent data leakage

2. **API Level**
   - Capability-based security model
   - Whitelisted tool access only
   - Rate limiting on all operations
   - Read-only database access

3. **Execution Level**
   - Resource limits (CPU, memory, time)
   - Separate execution context per script
   - No access to Python internals
   - Comprehensive audit logging

4. **Data Level**
   - Input sanitization
   - Output validation
   - No direct access to sensitive data
   - Encryption for stored scripts

### Security Configuration

```yaml
scripting:
  enabled: true

  cel:
    max_expression_length: 1000
    evaluation_timeout_ms: 100

  starlark:
    max_script_size: 10000
    execution_timeout_ms: 5000
    memory_limit_mb: 50

  security:
    allowed_tools:

      - get_weather
      - send_notification
      - query_calendar
    rate_limits:
      executions_per_minute: 10
      tool_calls_per_execution: 5
    audit:
      log_all_executions: true
      retain_logs_days: 30

```

## Migration Strategy

### For Event Listeners

```yaml

# Before: LLM-based condition
listeners:

  - name: "Temperature Alert"
    conditions:

      - source: home_assistant
        entity_id: sensor.outside_temperature
    action:
      type: wake_llm
      config:
        prompt: "Check if temperature > 30 and daytime"

# After: CEL expression
listeners:

  - name: "Temperature Alert"
    conditions:

      - source: home_assistant
        entity_id: sensor.outside_temperature
        cel_filter: "event.state > 30 && time.hour >= 6 && time.hour <= 22"
    action:
      type: notification
      config:
        template: "High temperature alert: {{event.state}}°C"

```

### For Complex Automations

```yaml

# Before: Multiple LLM calls
action:
  type: wake_llm
  config:
    prompt: "Check motion, determine if night, turn on lights if needed, schedule turn off"

# After: Starlark script
action:
  type: script
  config:
    language: starlark
    script_id: "motion_light_automation"

```

## Performance Considerations

### CEL Performance

- Expression evaluation: 10-100 microseconds
- Suitable for high-frequency event filtering
- Minimal memory overhead

### Starlark Performance

- Script execution: 1-10 milliseconds
- No Python GIL restrictions (parallel execution possible)
- Rust-based implementation provides excellent performance
- Memory usage proportional to script complexity
- Suitable for complex automations

### Optimization Strategies

1. Cache compiled expressions/scripts
2. Pre-validate scripts on save
3. Use CEL for hot paths, Starlark for complex logic
4. Monitor execution times and resource usage

## Success Metrics

1. **Cost Reduction**
   - Track LLM tokens saved by scripting
   - Measure percentage of automations handled by scripts

2. **Performance**
   - Script execution time percentiles
   - Resource usage statistics

3. **User Adoption**
   - Number of user-created scripts
   - Script execution frequency

4. **Security**
   - Zero security incidents from scripts
   - Audit log completeness

## Technical Implementation Notes

### Dependencies

Add to `pyproject.toml`:

```yaml
[project]
dependencies = [
    # ... existing dependencies
    "cel-python>=0.1.5",      # For simple expression evaluation
    "starlark-pyo3>=0.1.0",   # For complex scripting (Rust-based)
]

```

### Example Starlark Integration

```python
import starlark as sl

# Create execution environment
globals = sl.Globals.standard()
module = sl.Module()

# Add context APIs
module["event"] = event_data
module["time"] = TimeAPI()
module["db"] = DatabaseReadAPI(db_context)

# Parse and execute script
ast = sl.parse("user_script.star", script_code)
result = sl.eval(module, ast, globals)

```

## Conclusion

The hybrid CEL + Starlark approach provides the best balance of security, performance, and usability for Family Assistant's scripting needs. CEL handles simple expressions efficiently while Starlark (via starlark-pyo3) enables complex automations with Python-like syntax. The Rust-based implementation ensures excellent security and performance characteristics. The phased implementation allows for validation and refinement at each stage while maintaining system security and reliability.
