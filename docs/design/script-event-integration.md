# Script-Event Integration Design

## Overview

This document outlines the design for integrating the Starlark scripting engine with the event listener system, enabling users to write scripts that execute automatically in response to events from Home Assistant, document indexing, and other sources.

## Motivation

While the current `wake_llm` action is powerful for complex, context-aware responses, many automation use cases are deterministic and don't require LLM intelligence:

- Logging sensor data to notes
- Sending notifications based on thresholds
- Creating summary documents from emails
- Executing multi-step workflows on specific triggers
- Maintaining audit trails of home automation events

These use cases would benefit from:

- **Lower latency**- No LLM round-trip needed
- **Cost efficiency**- No API tokens consumed
- **Predictability**- Same input always produces same output
- **Reliability**- No dependency on external LLM service

## Design Goals

1. **Simplicity**: Natural extension of existing systems, minimal new concepts
2. **Security**: Scripts cannot exceed user's existing permissions
3. **Reliability**: Failures are isolated and don't break event processing
4. **Debuggability**: Clear logging, testing tools, and error messages
5. **Performance**: Scripts run asynchronously without blocking events
6. **Flexibility**: Support both simple and complex automation scenarios

## Anticipated Use Cases

### Home Automation

```starlark

# Log all motion events with timestamps
add_or_update_note(
    title=f"Motion Log - {time_format(time_now(), '%Y-%m-%d')}",
    content=f"Motion detected in {event['entity_id']} at {event['timestamp']}\n",
    append=True  # If this becomes available
)

# Temperature threshold with time-based logic
temp = float(event["new_state"]["state"])
hour = time_hour(time_now())
if temp > 25 and hour >= 9 and hour < 18:  # Business hours only
    send_telegram_message(message=f"🌡️ Office temperature: {temp}°C")

```

### Document Processing

```starlark

# Extract and summarize school newsletters
if event["source_id"] == "indexing" and "newsletter" in event["metadata"]["subject"]:
    # Fetch the full document content
    docs = search_documents(query=f"document_id:{event['document_id']}", limit=1)
    if docs:
        doc = json_decode(docs)[0]
        # Create a summary note with key dates
        add_or_update_note(
            title=f"Newsletter Summary - {time_format(time_now(), '%B %Y')}",
            content=f"Subject: {event['metadata']['subject']}\n\nKey dates: ..."
        )

```

### Multi-Step Workflows

```starlark

# Package delivery workflow
if event["entity_id"] == "sensor.front_door_package":
    # Log delivery
    add_or_update_note(title="Deliveries", content=f"Package detected at {time_now()}\n", append=True)

    # Check if anyone is home
    alex_home = get_entity_state(entity_id="person.alex")
    if alex_home and alex_home["state"] == "home":
        send_telegram_message(message="📦 Package delivered at front door!")

    # Update shopping list
    shopping = search_notes(query="Shopping List")
    # ... process and update

```

### Data Collection

```starlark

# Hourly energy usage tracking
reading = float(event["new_state"]["state"])
hour_str = time_format(time_now(), "%Y-%m-%d %H:00")

# Append to daily log
add_or_update_note(
    title=f"Energy Log - {time_format(time_now(), '%Y-%m-%d')}",
    content=f"{hour_str}: {reading} kWh\n",
    append=True
)

# Check for anomalies
if reading > 5.0:  # High usage threshold
    send_telegram_message(message=f"⚡ High energy usage: {reading} kWh")

```

## Technical Design

### 1. New Action Type

Add `SCRIPT` to the `EventActionType` enum:

```python
class EventActionType(str, Enum):
    WAKE_LLM = "wake_llm"
    SCRIPT = "script"  # New action type

```

This requires an Alembic migration to update the database enum.

### 2. Action Configuration Schema

The `action_config` JSON field for script actions:

```json
{
    "script_code": "# Starlark code here\nadd_or_update_note(...)",
    "timeout": 600  // Optional, defaults to 10 minutes
}

```

### 3. Script Execution Task

Create a new task type `script_execution` that runs via the task queue:

```python
async def handle_script_execution(
    exec_context: ToolExecutionContext,
    payload: dict[str, Any]
) -> None:
    """Execute a Starlark script triggered by an event."""
    script_code = payload["script_code"]
    event_data = payload["event_data"]
    config = payload.get("config", {})

    # Create script engine with restricted context
    engine = StarlarkEngine(
        tools_provider=exec_context.tools_registry,
        config=StarlarkConfig(
            execution_timeout=config.get("timeout", 600),
            allowed_tools=config.get("allowed_tools"),  # If not specified, uses event_handler profile
            deny_all_tools=False
        )
    )

    # Execute with event data as global
    try:
        result = await engine.evaluate(
            script_code,
            {"event": event_data, "conversation_id": exec_context.conversation_id}
        )
        logger.info(f"Script execution completed for listener {payload['listener_id']}")
    except ScriptTimeoutError:
        logger.error(f"Script timeout for listener {payload['listener_id']}")
        # Task will be retried automatically by task queue
        raise
    except ScriptError as e:
        logger.error(f"Script error for listener {payload['listener_id']}: {e}")
        # Task will be retried automatically by task queue
        raise

```

### 4. Event Processor Integration

Update `EventProcessor._execute_action()` to handle script actions:

```python
elif action_type == EventActionType.SCRIPT:
    # Queue script execution as a task
    await enqueue_task(
        db_context=db_ctx,
        task_type="script_execution",
        payload={
            "script_code": action_config["script_code"],
            "event_data": event_data,
            "config": action_config,
            "listener_id": listener["id"],
            "conversation_id": listener["conversation_id"],
        },
        conversation_id=listener["conversation_id"],
    )

```

### 5. Event Handler Processing Profile

Create a dedicated processing profile for event-triggered scripts with restricted tools:

```yaml
# config.yaml
service_profiles:
  - id: "event_handler"
    display_name: "Event Handler"
    llm_config:
      model: "claude-3-5-haiku-20241022"  # Use fast model for scripts
    tools_config:
      enable_local_tools:
        # Core data tools
        - "add_or_update_note"
        - "search_notes"
        - "search_documents"
        - "get_note"
        
        # Communication (no email sending to prevent spam)
        - "send_telegram_message"
        
        # Home Assistant (read-only)
        - "get_entity_state"
        - "get_all_entities"
        
        # Calendar (read-only)
        - "get_calendar_events"
        
        # No destructive operations:
        # - No delete_note
        # - No control_device (prevent automation loops)
        # - No send_email (prevent spam)
        # - No modify/delete calendar events
```

Scripts will use this profile's tools by default, inheriting safe defaults.

### 6. Tools for Script-Event Management

Extend existing event listener tools to support script actions:

```python
def create_event_listener(
    name: str,
    source: str,
    listener_config: dict,
    action_type: str = "wake_llm",  # New parameter
    script_code: str = None,  # New parameter
    script_config: dict = None,  # New parameter
    one_time: bool = False
) -> str:
    """Create an event listener with either wake_llm or script action."""

    if action_type == "script":
        if not script_code:
            raise ValueError("script_code required for script action type")

        listener_config["action_type"] = "script"
        listener_config["action_config"] = {
            "script_code": script_code,
            **(script_config or {})
        }

```

Add a test tool for scripts:

```python
def test_event_script(
    script_code: str,
    sample_event: dict,
    timeout: int = 5
) -> str:
    """Test a script with a sample event before creating a listener."""

```

### 6. Script Context and APIs

Scripts receive a rich context:

```starlark

# Global variables available to all event scripts:
# - event: The full event data dictionary
# - conversation_id: The conversation this listener belongs to
# - listener_name: The name of the listener that triggered this
# - trigger_count: How many times this listener has triggered today

# All standard APIs are available:
# - JSON functions (json_encode, json_decode)
# - Time API (time_now, time_format, etc.)
# - Tools (based on conversation permissions)

```

### 7. Wake LLM from Scripts

Scripts can wake the LLM when complex decision-making or context-aware responses are needed:

```starlark
# Wake LLM with simple context
temp = float(event["new_state"]["state"])
if temp > 30:
    wake_llm({
        "alert": "High temperature detected",
        "temperature": temp,
        "action_needed": "Check cooling system"
    })

# Multiple wake calls accumulate into single LLM wake
if humidity > 80:
    wake_llm({
        "sensor": "humidity",
        "value": humidity,
        "threshold": 80
    })

if air_quality < 50:
    wake_llm({
        "sensor": "air_quality", 
        "value": air_quality,
        "threshold": 50
    })
# LLM will be woken once with all accumulated contexts
```

**wake_llm API**:

- `wake_llm(context: dict, include_event: bool = True)` - Request to wake LLM with context
- Multiple calls within a script accumulate - LLM is woken at most once per script execution
- The `context` dict is passed to the LLM along with the original event data (if `include_event=True`)
- This enables hybrid automation: deterministic logic in scripts, complex decisions delegated to LLM

### 8. Script Limitations and Workarounds

**starlark-pyo3 Restriction**: Loops and certain constructs must be inside functions. This is actually beneficial for event handlers as it encourages modular code:

```starlark
# This will fail:
for event in events:
    process(event)

# This works:
def process_all():
    for event in events:
        process(event)
    return "processed"

# Execute the function
process_all()
```

For simple scripts, we could auto-wrap in a function, but the explicit requirement helps prevent accidentally complex top-level code.

### 9. Security Considerations

1. **Permission Model**: Scripts use the event_handler profile with restricted tools by default
2. **Resource Limits**:
   - Same timeout as manual scripts (10 minutes) to support long-running operations
   - Rate limiting applies to script-triggered tool calls
   - Note: Memory limits are not enforceable with starlark-pyo3
3. **Sandboxing**: No file system, network, or process access
4. **Audit Trail**: All script executions visible in task queue UI
5. **Rate Limiting**: Script actions count against the same daily limits as wake_llm actions

### 10. Error Handling

Script execution tasks use the standard task queue retry mechanism:

- Automatic retry with exponential backoff
- Alert user if final retry fails
- All execution history visible in task UI

### 11. Script Validation and Testing

1. **Syntax Validation**: Scripts are linted before saving:

   ```python
   try:
       # Parse the script to check syntax
       starlark.parse(script_code)
   except starlark.SyntaxError as e:
       raise ValueError(f"Script syntax error at line {e.line}: {e.msg}")
   ```

2. **Syntax Validation Only** (dry run may be added later):

   ```python
   def validate_event_script(script_code: str) -> str:
       """Validate script syntax before creating a listener."""
       try:
           import starlark
           starlark.parse(script_code)
           return json.dumps({"success": True, "message": "Script syntax is valid"})
       except Exception as e:
           return json.dumps({"success": False, "error": f"Syntax error: {str(e)}"})
   ```

3. **Task History Debugging**: All script executions appear in task history with:
   - Full script code
   - Event that triggered it
   - Tool calls made
   - Execution time
   - Any errors with line numbers

## Storage Considerations

Scripts are stored directly in the `action_config` JSON field. There's more than enough space for any reasonable automation script.

## Implementation Plan

### Phase 1: Core Infrastructure

1. Add `SCRIPT` to `EventActionType` enum (with migration)
2. Create `event_handler` processing profile with restricted tools
3. Create `script_execution` task handler with syntax validation
4. Update `EventProcessor` to support script actions
5. Basic testing with hardcoded scripts

### Phase 2: Tool Integration

1. Extend `create_event_listener` tool for scripts
2. Add `validate_event_script` tool for syntax checking
3. Integrate with task history for debugging
4. Update web UI to show script listeners

### Phase 3: Production Hardening

1. Use standard task retry mechanism
2. Alert user on final retry failure
3. Performance monitoring and alerts
4. Documentation and examples

## Migration Path and Backward Compatibility

1. **No Breaking Changes**: Existing `wake_llm` listeners continue to work unchanged
2. **Gradual Migration**: Users can convert suitable automations to scripts over time
3. **Mixed Usage**: Some listeners can use `wake_llm` (for complex logic) while others use scripts
4. **Database Compatible**: The enum addition is backward compatible with existing rows

Example migration conversation:

```
User: "I have a listener that wakes you whenever motion is detected just to log it"
Assistant: "I can convert that to a script for faster execution and no API usage. Would you like me to do that?"

```

## Future Enhancements

1. **Dry Run Capability**: Test scripts with mock tool responses
   - starlark-pyo3 exception handling behavior:
     - All Python exceptions are wrapped in StarlarkError
     - Original exception type and message preserved as: `error: <ExceptionType>: <message>`
     - Includes Starlark traceback with line numbers and visual indicators
     - Python traceback is not included, only exception type and message
     - Custom exception attributes are lost
   - Implementation approach:
     - Could use a custom exception type (e.g., `DryRunStop`) thrown after first tool
     - Script would catch it as `error: DryRunStop: <details>`
     - Would need to handle in StarlarkEngine.evaluate() error processing
   - Would show what tools would be called with what arguments

2. **Append Mode for Notes**: Add `append` parameter to `add_or_update_note`:

   ```starlark
   add_or_update_note(title="Log", content="New entry\n", append=True)
   ```

3. **Tool-level Rate Limiting**: Separate rate limits for tool calls vs script executions

4. **Script Library**: Pre-built templates for common patterns (maybe)

5. **State Storage**: If concrete use cases emerge for persistent script state

## Example: Complete Temperature Monitoring Automation

```python

# User: "Run a script whenever the server room temperature changes"
# Assistant creates listener with this script:

temp = float(event["new_state"]["state"])
prev_temp = float(event["old_state"]["state"]) if event["old_state"] else 0

# Log all changes
add_or_update_note(
    title=f"Server Room Temperature Log - {time_format(time_now(), '%Y-%m-%d')}",
    content=f"{time_format(time_now(), '%H:%M')} - {temp}°C (was {prev_temp}°C)\n",
    append=True
)

# Alert on high temperature
if temp > 28 and prev_temp <= 28:
    send_telegram_message(
        message=f"🔥 Server room temperature critical: {temp}°C"
    )
    # Could also: control_device(entity_id="switch.server_room_fan", action="turn_on")

# Critical temperature - wake LLM for complex response
if temp > 35:
    wake_llm({
        "alert_type": "critical_temperature",
        "location": "server_room",
        "temperature": temp,
        "previous_temperature": prev_temp,
        "instruction": "Analyze the situation and take appropriate emergency actions"
    })

# All-clear notification
elif temp <= 25 and prev_temp > 25:
    send_telegram_message(
        message=f"✅ Server room temperature normal: {temp}°C"
    )

```

## Success Criteria

1. Users can create script-based automations via natural language
2. Scripts execute reliably without blocking event processing
3. Failed scripts don't break the event system
4. Clear debugging tools help users understand script behavior
5. Performance is measurably better than wake_llm for simple tasks

## Implementation Progress

### ✅ Phase 1: Core Infrastructure (Complete)

1. **Database Schema** ✓
   - Added `SCRIPT` to `EventActionType` enum
   - Created Alembic migration (`add_script_enum`)
   - Updated event listeners table to support script action type

2. **Event Handler Profile** ✓
   - Created `event_handler` processing profile in config
   - Configured with restricted tool access for safety
   - Using fast Haiku model for script execution

3. **Script Execution Task** ✓
   - Implemented `handle_script_execution` in task_worker.py
   - Full StarlarkEngine integration with tool access
   - Proper error handling and retry mechanism
   - Comprehensive logging for debugging

4. **Event Processor Integration** ✓
   - Updated EventProcessor to handle script actions
   - Scripts execute via task queue (non-blocking)
   - Proper task ID generation and payload structure

5. **Testing Infrastructure** ✓
   - Created comprehensive functional tests in `test_script_execution_handler.py`
   - Tests cover: successful execution, syntax errors, multiple tool calls
   - Fixed test helper `wait_for_tasks_to_complete` to filter by task type
   - All tests passing reliably

### ✅ Phase 2: Tool Integration (Complete)

1. **Tool Updates** ✓
   - Extended `create_event_listener` tool to support script actions
   - Added `validate_event_listener_script` tool for syntax checking
   - Added `test_event_listener_script` tool for dry runs
   - All tools fully tested with comprehensive integration tests

2. **Web UI** (TODO)
   - Update event listeners UI to show script code
   - Add script editor with syntax highlighting
   - Show script execution history in task UI

### 📋 Phase 3: Production Hardening (Future)

1. **Monitoring** (TODO)
   - Performance metrics for script execution
   - Alert thresholds for failed scripts
   - Script execution analytics

2. **Documentation** (TODO)
   - User guide for writing event scripts
   - Common patterns and examples
   - Troubleshooting guide

### Key Implementation Details

1. **Script Context**: Scripts receive:
   - `event`: Full event data dictionary
   - `conversation_id`: Associated conversation
   - `listener_id`: ID of triggering listener
   - Access to all time/date APIs and allowed tools

2. **Security Model**:
   - Scripts use event_handler profile tools only
   - No destructive operations by default
   - 10-minute timeout for long-running operations
   - Full audit trail in task history

3. **Error Handling**:
   - Syntax errors prevent script creation
   - Runtime errors trigger task retry with backoff
   - Clear error messages with line numbers
   - Failed scripts don't affect other listeners

## Conclusion

This integration provides a powerful automation capability while maintaining the simplicity and security of the existing system. By leveraging the task queue and existing permission model, we can add scripting without major architectural changes. The design prioritizes user safety and system reliability while enabling sophisticated automation workflows.

Phase 1 is now complete with full test coverage. The core infrastructure is ready for tool integration and UI enhancements in Phase 2.
