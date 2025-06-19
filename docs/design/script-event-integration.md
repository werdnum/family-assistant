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
    "timeout": 600,  // Optional, defaults to 10 minutes for event scripts (same as manual scripts)
    "allowed_tools": ["add_or_update_note", "search_notes"],  // Optional
    "fail_strategy": "retry_and_alert"  // or "log_and_continue" or "alert_only"
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
        # Handle based on fail_strategy
    except ScriptError as e:
        logger.error(f"Script error for listener {payload['listener_id']}: {e}")
        # Handle based on fail_strategy

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

### 7. Script Limitations and Workarounds

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

### 8. Security Considerations

1. **Permission Model**: Scripts use the event_handler profile with restricted tools by default
2. **Resource Limits**:
   - Same timeout as manual scripts (10 minutes) to support long-running operations
   - Rate limiting applies to script-triggered tool calls
   - Script size limited to 10KB to prevent storage abuse
   - Note: Memory limits are not enforceable with starlark-pyo3
3. **Sandboxing**: No file system, network, or process access
4. **Audit Trail**: All script executions logged with results
5. **Rate Limiting**: Script actions count against the same daily limits as wake_llm actions

### 9. Error Handling

Three failure strategies configurable per listener:

1. **retry_and_alert** (default): Retry with exponential backoff (max 3 attempts), alert user on final failure
2. **log_and_continue**: Log error, mark task failed, continue processing (for non-critical scripts)
3. **alert_only**: Alert user immediately on first failure (for critical scripts)

### 10. Script Validation and Testing

1. **Syntax Validation**: Scripts are linted before saving:

   ```python
   try:
       # Parse the script to check syntax
       starlark.parse(script_code)
   except starlark.SyntaxError as e:
       raise ValueError(f"Script syntax error at line {e.line}: {e.msg}")
   ```

2. **Limited Dry Run**: Test scripts up to first tool call:

   ```python
   # Mock tools that log calls instead of executing
   mock_provider = MockToolProvider(log_only=True)
   engine = StarlarkEngine(tools_provider=mock_provider)
   result = await engine.evaluate(script_code, test_event)
   # Returns: {"tool_calls": ["add_or_update_note", "send_telegram_message"]}
   ```

3. **Task History Debugging**: All script executions appear in task history with:
   - Full script code
   - Event that triggered it
   - Tool calls made
   - Execution time
   - Any errors with line numbers

4. **Auto-disable on Repeated Failures**: After 5 consecutive failures, listener is disabled with user notification

## Storage Considerations

Since scripts are stored in the `action_config` JSON field:

1. **Size Limits**: 10KB max script size (enforced at creation time)
2. **Validation**: Scripts are syntax-checked before storage
3. **Compression**: Large scripts could be gzip-compressed if needed (future enhancement)
4. **Separate Table**: If scripts grow beyond JSON limits, consider separate script storage table (future)

For MVP, storing scripts directly in `action_config` is sufficient given the 10KB limit.

## Implementation Plan

### Phase 1: Core Infrastructure

1. Add `SCRIPT` to `EventActionType` enum (with migration)
2. Create `event_handler` processing profile with restricted tools
3. Create `script_execution` task handler with syntax validation
4. Update `EventProcessor` to support script actions
5. Basic testing with hardcoded scripts

### Phase 2: Tool Integration

1. Extend `create_event_listener` tool for scripts
2. Add `test_event_script` tool with limited dry-run capability
3. Integrate with task history for debugging
4. Update web UI to show script listeners

### Phase 3: Production Hardening

1. Implement retry_and_alert error handling strategy
2. Add auto-disable after repeated failures
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

1. **Wake LLM from Script**: Special tool to wake the LLM with filtered context:

   ```starlark
   if complex_condition:
       wake_llm(prompt="Handle this complex situation", context=event)
   ```

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

## Conclusion

This integration provides a powerful automation capability while maintaining the simplicity and security of the existing system. By leveraging the task queue and existing permission model, we can add scripting without major architectural changes. The design prioritizes user safety and system reliability while enabling sophisticated automation workflows.
