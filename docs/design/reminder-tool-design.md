# Dedicated Reminder Tool Design

## Problem Statement

The current callback tool with `skip_if_user_responded` parameter is not being used consistently by the LLM for reminder functionality. Common issues include:
- LLM sets `skip_if_user_responded=true` on initial reminders, causing them to never be sent
- LLM forgets to schedule follow-up reminders in callback handlers
- Confusion between generic callbacks and reminder-specific functionality

## Solution: Dedicated `schedule_reminder` Tool

### Overview

Create a purpose-built reminder tool that handles the complete reminder lifecycle automatically, removing the need for the LLM to orchestrate multiple callbacks.

### Tool Definition

```python
schedule_reminder(
    reminder_time: str,  # ISO 8601 datetime with timezone
    message: str,        # The reminder message to send
    follow_up: bool = False,  # Whether to send follow-up reminders if no response
    follow_up_interval: str = "30 minutes",  # Time between follow-ups (e.g., "30 minutes", "1 hour")
    max_follow_ups: int = 2,  # Maximum number of follow-up reminders
) -> dict
```

### Key Differences from Callback Tool

1. **Semantic Clarity**: `schedule_reminder` vs `schedule_future_callback`
2. **Automatic Follow-ups**: System handles all follow-up logic
3. **No Manual Skip Logic**: System automatically determines if user has acknowledged
4. **Reminder-Specific Parameters**: Clear options for nagging behavior

### Implementation Details

#### Database Schema

Add to the existing tasks table:
- `task_type`: Use new type `"reminder"` (vs existing `"llm_callback"`)
- `payload`: Store reminder configuration including follow-up settings
- `metadata`: Track reminder state (attempts, last_sent, user_acknowledged)

#### Task Processing

1. **Initial Reminder**:
   - Send the reminder message at scheduled time
   - If `follow_up=true`, automatically schedule next reminder
   - Track message ID for response detection

2. **Follow-up Logic**:
   - Check if user has sent any messages since last reminder
   - Analyze if user's response acknowledges the reminder
   - If not acknowledged and attempts < max_follow_ups, send follow-up
   - If acknowledged, mark complete and cancel future follow-ups

3. **Acknowledgment Detection**:
   - Simple: Any user message cancels follow-ups
   - Advanced (future): Use LLM to determine if response acknowledges task completion

#### LLM Wake-up for Follow-ups

When a follow-up is triggered:
- System wakes the LLM with context about the original reminder
- LLM can see conversation history since last reminder
- LLM crafts appropriate follow-up message based on context
- Allows for natural progression (e.g., "Just following up on...", "This is your second reminder about...")

### Implementation Architecture

#### Shared Task Implementation

Both reminder and callback tools can share the same underlying task implementation with different configurations:

```python
# Unified task structure
{
    "task_type": "llm_trigger",  # Single type for both
    "trigger_config": {
        "trigger_type": "callback" | "reminder",
        "message": str,  # Context/instructions for LLM
        "reminder_config": {  # Only present for reminders
            "follow_up": bool,
            "follow_up_interval": str,
            "max_follow_ups": int,
            "attempts": int,  # Track current attempt number
        }
    }
}
```

Benefits of shared implementation:
- Reuse existing `handle_llm_callback` logic
- Single code path for LLM wake-up
- Easier to maintain and test
- Natural migration path

The task handler would:
1. Wake LLM with appropriate context
2. For reminders with follow-up enabled:
   - Check if user responded since last trigger
   - If not, and attempts < max, schedule next follow-up
   - Pass attempt count to LLM for context

#### LLM Context for Wake-ups

**For Callbacks:**
```
System: Scheduled callback triggered
Context: [original callback context provided by LLM]
```

**For Reminders (initial):**
```
System: Reminder triggered
Task: Send a reminder about: [original reminder message]
```

**For Reminders (follow-up):**
```
System: Follow-up reminder triggered (attempt 2 of 3)
Original reminder: [original reminder message]
Note: User has not responded to previous reminder sent at [timestamp]
```

### Changes to Existing Tools

#### Callback Tool
- Remove `skip_if_user_responded` parameter
- Update description to clarify it's for generic LLM re-engagement, not reminders
- Examples should focus on non-reminder use cases (e.g., "check back on long-running task")

#### System Prompt Updates

Replace current reminder instructions (lines 17-18 in prompts.yaml) with:

```yaml
  * For reminders:
    * Use the `schedule_reminder` tool when users ask to be reminded of something
    * Set `follow_up=true` for important reminders or when user says "don't let me forget"
    * Use `schedule_future_callback` for continuing work or checking on tasks, not for reminders
    * Examples:
      - "Remind me to call mom" → schedule_reminder with follow_up=false
      - "Don't let me forget the meeting" → schedule_reminder with follow_up=true
      - "Check if the download finished in an hour" → schedule_future_callback
```

### Migration Strategy

1. **Phase 1**: Implement new tool alongside existing callback tool
2. **Phase 2**: Update prompts to guide LLM to use new tool
3. **Phase 3**: Monitor usage and refine based on LLM behavior
4. **Phase 4**: Consider deprecating reminder functionality from callback tool

### Tool Usage Examples

#### Simple Reminder
```python
# User: "Remind me to take my medication at 3pm"
schedule_reminder(
    reminder_time="2024-01-10T15:00:00-05:00",
    message="Time to take your medication!",
    follow_up=False
)
```

#### Persistent Reminder
```python
# User: "Don't let me forget to submit the report today"
schedule_reminder(
    reminder_time="2024-01-10T14:00:00-05:00",
    message="Don't forget to submit the report today!",
    follow_up=True,
    follow_up_interval="1 hour",
    max_follow_ups=3
)
```

### Future Enhancements

1. **Smart Acknowledgment**: Use LLM to determine if user's response indicates task completion
2. **Snooze Functionality**: Allow users to postpone reminders
3. **Escalation**: Different follow-up messages or channels for ignored reminders
4. **Templates**: Pre-defined reminder patterns for common use cases
5. **Integration with Event System**: As outlined in FUTURE_PLANS.md

### Testing Considerations

1. **Unit Tests**:
   - Reminder scheduling
   - Follow-up generation
   - User response detection

2. **Integration Tests**:
   - Full reminder lifecycle
   - Interaction with message history
   - Concurrent reminders

3. **LLM Behavior Tests**:
   - Correct tool selection
   - Parameter usage
   - Edge cases (overlapping reminders, etc.)

### Test Implementation

A comprehensive test `test_schedule_reminder_with_follow_up` has been added to `tests/functional/test_smoke_callback.py` that covers:
- Scheduling a reminder with follow-up enabled
- Verifying reminder configuration in database
- Executing initial reminder and verifying message sent
- Automatic scheduling of follow-up reminder
- Executing follow-up reminders
- Verifying max follow-ups limit is respected
- User response interaction

The previous `test_callback_skip_behavior_on_user_response` test has been removed as the `skip_if_user_responded` parameter was removed from the callback tool.

### Open Questions

1. How to handle reminders when user is in active conversation?
2. Should we support reminder modification after creation?
3. Maximum time limit for follow-ups (e.g., stop after 24 hours)?
4. Should we use a single `task_type` (e.g., "llm_trigger") or keep separate types for database clarity?
5. For shared implementation, should the reminder config be in the top-level payload or nested?