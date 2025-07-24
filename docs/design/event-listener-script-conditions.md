# Event Listener Script Conditions Design

## Overview

This document describes the design for adding Starlark script-based conditions to the event listener
system. This enhancement allows complex event matching logic beyond simple dictionary equality,
while maintaining backward compatibility and security.

## Problem Statement

The current event listener system uses simple dictionary equality matching, which cannot handle:

- State transitions (e.g., zone entry/exit detection)
- Attribute comparisons (e.g., temperature > threshold)
- Complex boolean logic (e.g., (A or B) and not C)
- Relative comparisons between old and new states

Home Assistant emits `state_changed` events even when only attributes change, making it impossible
to detect actual state transitions using simple equality.

## Design Goals

1. **Backward Compatibility**: Existing dict-based listeners must continue working
2. **Security**: Scripts must be sandboxed with no access to tools, files, or network
3. **Performance**: Scripts must execute quickly (\<100ms) on every event
4. **Simplicity**: Common cases should remain simple, complex cases should be possible
5. **Debuggability**: Clear error messages and testing capabilities

## Proposed Solution

### Database Schema Changes

Add a new column to the `event_listeners` table:

```sql
ALTER TABLE event_listeners ADD COLUMN condition_script TEXT;
```

The script is optional. When present, it overrides `match_conditions` for backward compatibility.

### Script Execution Model

1. **Input**: Scripts receive a single `event` variable containing the full event data
2. **Output**: Scripts must evaluate to a boolean value (true = match, false = no match)
3. **Environment**: Minimal Starlark environment with no tool access
4. **Format**: Scripts can be either:
   - Simple expressions (e.g., `event.get('state') == 'on'`)
   - Multi-line scripts with `return` statements (automatically wrapped in a function)

### Script Environment

```python
# Available to scripts:
event = {
    "event_type": "state_changed",
    "entity_id": "person.alex_smith",
    "old_state": {
        "state": "not_home",
        "attributes": {...},
        "last_changed": "2024-01-07T10:00:00Z"
    },
    "new_state": {
        "state": "home",
        "attributes": {...},
        "last_changed": "2024-01-07T10:30:00Z"
    }
}

# Built-in functions (minimal set):
- Standard Starlark built-ins (len, str, int, bool, dict, list)
- Note: float() is not available in Starlark, use int() for numeric conversions
- No print, no file access, no network
- No tool execution
- No wake_llm
```

### Security Model

1. **Sandboxed Environment**:

   - Create a separate `StarlarkConfig` with all features disabled
   - No tool provider access
   - No global variables except `event`
   - No imports allowed

2. **Resource Limits**:

   - Execution timeout: 100ms (configurable in config.yaml under
     `event_system.script_execution_timeout_ms`)
   - Memory limit: 10MB per script
   - Script size limit: 10KB (configurable in config.yaml under
     `event_system.script_size_limit_bytes`)
   - Complexity limit: No loops over 1000 iterations

3. **Validation**:

   - Parse and compile on creation
   - Test execution with sample event data
   - Verify return type is boolean

### Implementation Architecture

```python
class EventConditionEvaluator:
    """Evaluates Starlark condition scripts for event matching."""
    
    def __init__(self, config: dict[str, Any] | None = None):
        # Configuration for event condition evaluation
        timeout_ms = (config or {}).get('script_execution_timeout_ms', 100)
        self.timeout = timeout_ms / 1000.0  # Convert to seconds
        self.size_limit = (config or {}).get('script_size_limit_bytes', 10240)
    
    def evaluate_condition(
        self, 
        script: str, 
        event_data: dict[str, Any]
    ) -> bool:
        """Evaluate a condition script against event data.
        
        Scripts are Starlark expressions (not full programs) that have
        access to the 'event' variable and must evaluate to a boolean.
        """
        import starlark
        
        # Create a module with the event data
        module = starlark.Module()
        module["event"] = event_data
        
        # Wrap the expression in an assignment
        wrapped_script = f"_result = ({script})"
        
        # Parse and evaluate
        ast = starlark.parse("condition.star", wrapped_script)
        starlark.eval(module, ast, starlark.Globals.standard())
        
        # Get and validate result
        result = module["_result"]
        if not isinstance(result, bool):
            raise ScriptExecutionError(
                f"Script must return boolean, got {type(result)}"
            )
        
        return result
```

### EventProcessor Integration

```python
# In EventProcessor._check_match_conditions
def _check_match_conditions(
    self, 
    event_data: dict, 
    match_conditions: dict | None,
    condition_script: str | None
) -> bool:
    """Check if event matches the listener's conditions."""
    # Script takes precedence if present
    if condition_script:
        try:
            return self.condition_evaluator.evaluate_condition(
                condition_script, 
                event_data
            )
        except Exception as e:
            logger.error(f"Script condition error: {e}")
            return False  # Failed scripts don't match
    
    # Fall back to dict matching
    return self._check_dict_conditions(event_data, match_conditions)
```

### Common Script Patterns

#### Zone Entry Detection

```python
# Person arrives home (single expression)
event.get("old_state", {}).get("state") != "home" and event.get("new_state", {}).get("state") == "home"
```

#### Zone Exit Detection

```python
# Person leaves home (single expression)
event.get("old_state", {}).get("state") == "home" and event.get("new_state", {}).get("state") != "home"
```

#### Any State Change

```python
# Detect actual state changes (not attribute-only updates)
event.get("old_state", {}).get("state") != event.get("new_state", {}).get("state")
```

#### Temperature Threshold

```python
# Temperature increased by more than 5 degrees
# Note: Starlark doesn't have float(), so use int() for numeric comparisons
# For decimal values, consider multiplying by 10 or 100 before converting to int
int(event.get("new_state", {}).get("state", "0")) > int(event.get("old_state", {}).get("state", "0")) + 5
```

#### Complex Conditions

```python
# Motion detected (expression form)
event.get("entity_id", "").startswith("binary_sensor.") and event.get("entity_id", "").endswith("_motion") and event.get("new_state", {}).get("state") == "on"

# Note: More complex logic requiring if/else statements or multiple steps
# cannot be expressed as single expressions. Use multiple listeners or
# combine with Home Assistant's own conditions for complex scenarios.
```

### Tool Integration

Update the `create_event_listener` tool:

```python
def create_event_listener(
    name: str,
    source: str,
    listener_config: dict,
    condition_script: str | None = None,  # New parameter
    one_time: bool = False
) -> str:
    """
    Create an event listener.
    
    Args:
        condition_script: Optional Starlark expression for complex matching.
                         Has access to 'event' variable, must evaluate to boolean.
                         Should be a single expression, not a full script.
                         Overrides match_conditions if provided.
    """
    # Validate script if provided
    if condition_script:
        validator = EventConditionValidator()
        validation_result = validator.validate_script(condition_script)
        if not validation_result.valid:
            return json.dumps({
                "success": False,
                "error": f"Invalid script: {validation_result.error}"
            })
```

### Script Validation

```python
class EventConditionValidator:
    """Validates condition scripts before saving."""
    
    def __init__(self, evaluator: EventConditionEvaluator | None = None, config: dict[str, Any] | None = None):
        # Use provided evaluator or create one
        # This allows sharing the evaluator instance and its cache
        self.evaluator = evaluator or EventConditionEvaluator(config)
        self.size_limit = (config or {}).get('script_size_limit_bytes', 10240)
    
    def validate_script(self, script: str) -> ValidationResult:
        """Validate a condition script."""
        # Check size
        if len(script) > self.size_limit:
            return ValidationResult(
                valid=False,
                error=f"Script too large (max {self.size_limit} bytes)"
            )
        
        # Try to parse and execute
        try:
            # Test with sample event
            sample_event = {
                "entity_id": "test.entity",
                "old_state": {"state": "off"},
                "new_state": {"state": "on"}
            }
            result = self.evaluator.evaluate_condition(script, sample_event)
            # evaluate_condition already checks for boolean result
        except ScriptSyntaxError as e:
            return ValidationResult(
                valid=False,
                error=f"Syntax error: {e}"
            )
        except Exception as e:
            return ValidationResult(
                valid=False,
                error=f"Validation error: {e}"
            )
        
        return ValidationResult(valid=True)
```

### Error Handling

1. **Script Errors Don't Crash System**:

   - Failed scripts return `false` (no match)
   - Errors are logged but don't stop event processing
   - Listeners with broken scripts effectively become disabled

2. **User Feedback**:

   - Validation errors shown on creation
   - Runtime errors logged and available via status tool
   - Test tool can help debug scripts

### Performance Considerations

1. **Script Caching**:

   - Compile scripts once and cache the AST
   - Cache invalidation on listener update

2. **Execution Limits**:

   - 100ms timeout per script
   - Scripts that timeout repeatedly get disabled

3. **Monitoring**:

   - Track script execution times
   - Log slow scripts for optimization

### Testing Strategy

1. **Unit Tests**:

   - Test script evaluation with various event types
   - Test security restrictions (no tool access)
   - Test timeout enforcement

2. **Integration Tests**:

   - End-to-end event → script → action flow
   - Performance testing with many scripts

3. **Script Test Tool**:

   ```python
   def test_event_listener_script(
       script: str,
       sample_events: list[dict],
   ) -> str:
       """Test a condition script against sample events."""
   ```

## Implementation Plan

### Phase 1: Core Infrastructure ✅ COMPLETED

1. ✅ Add `condition_script` column to database - Created migration
2. ✅ Create `EventConditionEvaluator` class - Implemented in `events/condition_evaluator.py`
3. ✅ Integrate with `EventProcessor` - Updated to check scripts before dict conditions
4. ✅ Add script validation - `EventConditionValidator` validates syntax and return type

### Phase 2: Tool Integration ✅ COMPLETED

1. ✅ Update `create_event_listener` tool - Added `condition_script` parameter
2. ✅ Add script parameter handling - Scripts validated before saving
3. ✅ Implement validation in tool - Rejects invalid scripts with clear errors
4. ✅ Update tool documentation - Added examples and parameter descriptions

### Phase 3: Testing & Hardening ✅ COMPLETED

1. ✅ Add comprehensive test suite - Unit and integration tests in `tests/functional/events/`
2. ⏳ Performance benchmarking - Deferred to production usage
3. ⏳ Security audit - Basic sandboxing implemented, formal audit deferred
4. ✅ Documentation updates - This document serves as primary documentation

### Phase 4: Enhanced Features (Future)

1. Add time/date functions to script environment
2. Add helper functions for common patterns
3. Script templates in UI
4. Debugging aids

## Implementation Status

The event listener script conditions feature has been successfully implemented with the following
components:

- **Database**: Added `condition_script` TEXT column to `event_listeners` table
- **Evaluator**: `EventConditionEvaluator` class provides sandboxed Starlark execution
- **Processor**: `EventProcessor` checks scripts before dict conditions, with error handling
- **Tools**: `create_event_listener` tool accepts and validates scripts
- **Tests**: Comprehensive unit and integration tests ensure correctness
- **Security**: Scripts run in restricted environment with no tool/file/network access

## Migration Path

1. Existing listeners continue using `match_conditions`
2. New listeners can optionally use `condition_script`
3. If both are present, script takes precedence
4. Future: Tool to convert simple conditions to scripts

## Alternative Considered

We considered adding operators like `$ne`, `$gt`, etc. to the dict matching system, but rejected
this because:

- It creates a mini query language that will grow over time
- It's less powerful than real scripting
- It's harder to understand and debug
- We already have Starlark infrastructure

## Security Review Checklist

- [ ] No file system access in scripts
- [ ] No network access in scripts
- [ ] No tool execution from scripts
- [ ] No imports allowed
- [ ] Execution timeout enforced
- [ ] Memory limits enforced
- [ ] Return type validation
- [ ] Script size limits
- [ ] No infinite loops possible
- [ ] No access to other listeners' data

## Configuration

The script execution parameters can be configured in `config.yaml`:

```yaml
event_system:
  # Script execution limits
  script_execution_timeout_ms: 100  # Maximum execution time in milliseconds
  script_size_limit_bytes: 10240    # Maximum script size (10KB default)
  script_cache_size: 100            # Number of compiled scripts to cache
  
  # Security settings
  script_max_iterations: 1000       # Maximum loop iterations allowed
  script_memory_limit_mb: 10        # Memory limit per script execution
```

## Open Questions

1. Should we add basic time functions for time-based conditions?

   - Pro: Enables "only at night" conditions
   - Con: Increases complexity and security surface

2. Should scripts be able to access other entities' states?

   - Pro: Enables "when A and B are both on" conditions
   - Con: Performance and security concerns

3. Should we provide a library of common script templates?

   - Pro: Easier for users
   - Con: Maintenance burden

## Conclusion

Adding Starlark script conditions provides a powerful, flexible solution for complex event matching
while maintaining the simplicity of dict matching for basic cases. The sandboxed execution
environment ensures security, and the integration with existing infrastructure minimizes
implementation complexity.
