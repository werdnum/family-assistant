# Event Listener System Design

## Overview

The event listener system will enable the assistant to react to events from various sources (Home Assistant, email arrivals, document indexing completion, etc.) and trigger appropriate actions or notifications. It's designed to handle lightweight, one-off automations that benefit from LLM intelligence and integration with the assistant's existing tools.

### Use Cases

- "Tell me when Andrew gets home"
- "Turn on the lights when the camera detects motion"
- "Send me a summary when the school newsletter comes in by email"
- "Let me know when this document is finished indexing"
- "Alert me if the server room temperature exceeds 30°C"
- "Every Monday morning, remind me to check the task list"

### Design Principles

1. **Simplicity**: Easy to create listeners via natural language
2. **Flexibility**: Support various event sources and actions
3. **Extensibility**: Plugin architecture for new sources/actions
4. **Safety**: Sandboxed filtering, no arbitrary code execution
5. **Integration**: Leverage existing task queue and tool infrastructure

## Architecture

### 1. Event Sources

Event sources are pluggable components that monitor external systems and emit events:

```python
# src/family_assistant/events/sources.py
class EventSource(Protocol):
    """Base protocol for event sources"""
    
    async def start(self) -> None:
        """Start listening for events"""
        ...
    
    async def stop(self) -> None:
        """Stop listening for events"""
        ...
    
    @property
    def source_id(self) -> str:
        """Unique identifier for this source"""
        ...
```

#### Initial Event Sources

1. **Home Assistant Events** (`home_assistant`)
   - WebSocket connection to HA event bus
   - Source-level filtering by entity_id patterns
   - Subdivisions: `state_changed`, `automation_triggered`, etc.
   - Example events: motion detected, door opened, temperature threshold

2. **Document Indexing Events** (`indexing`)
   - Internal events from indexing pipeline
   - Subdivisions by document type: `email`, `pdf`, `note`
   - Triggers when documents complete processing with metadata
   - Example: "School newsletter email indexed with subject/sender"

3. **Webhook Events** (`webhook`)
   - HTTP endpoint for custom integrations
   - Subdivisions by webhook path or header
   - Allows external systems to push events
   - Example: "Build completed", "Package delivered"

### 2. Event Listener Configuration

Stored in database with simple dictionary matching for event filtering:

```sql
CREATE TABLE event_listeners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- Internal DB ID
    name VARCHAR(100) NOT NULL,
    description TEXT,
    source_id VARCHAR(50) NOT NULL,  -- Uses EventSourceType enum
    match_conditions JSON NOT NULL,  -- Dict including entity_id and other conditions
    action_type VARCHAR(50) NOT NULL DEFAULT 'wake_llm',  -- Uses EventActionType enum
    action_config JSON,       -- Configuration for the action (flexible structure)
    conversation_id VARCHAR(255) NOT NULL,  -- Matches message_history.conversation_id
    interface_type VARCHAR(50) NOT NULL DEFAULT 'telegram',  -- Matches message_history.interface_type
    one_time BOOLEAN DEFAULT FALSE,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Rate limiting fields
    daily_executions INTEGER DEFAULT 0,
    daily_reset_at TIMESTAMP,
    last_execution_at TIMESTAMP,
    UNIQUE(name, conversation_id),  -- Prevent duplicate names per conversation
    INDEX idx_source_enabled (source_id, enabled),
    INDEX idx_conversation (conversation_id, enabled)
);

-- Event storage for debugging (queryable via tool)
CREATE TABLE recent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id VARCHAR(100) UNIQUE NOT NULL,
    source_id VARCHAR(50) NOT NULL,  -- Uses EventSourceType enum
    event_data JSON NOT NULL,  -- Full event including entity_id
    triggered_listener_ids JSON,  -- Array of listener IDs that were triggered
    timestamp TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_source_time (source_id, timestamp),
    INDEX idx_created (created_at)  -- For efficient cleanup
);
```

**Schema Design Decisions:**

1. **Single `id` field**: The `id` field serves as the unique identifier. We use `UNIQUE(name, conversation_id)` to ensure user-friendly names are unique per conversation.

2. **Moved `entity_id` into `match_conditions`**: This reduces the API surface area and allows more flexibility. Example:
   ```json
   {"entity_id": "person.andrew", "new_state.state": "home"}
   ```

3. **Removed `time_constraints`**: Too specific for MVP. Complex conditions can be added later or handled by the LLM when woken.

4. **Removed `created_by`**: Not needed for MVP functionality.

5. **Added `conversation_id` and `interface_type`**: These match the schema in `message_history_table` to properly identify which conversation to wake.

6. **Flexible `action_config`**: JSON field allows different action types to define their own configuration structure.

7. **SQLAlchemy Enum for `source_id`**:
   ```python
   class EventSourceType(str, Enum):
       HOME_ASSISTANT = "home_assistant"
       INDEXING = "indexing"
       WEBHOOK = "webhook"
   ```

8. **SQLAlchemy Enum for `action_type`**:
   ```python
   class EventActionType(str, Enum):
       WAKE_LLM = "wake_llm"
       # Future: TOOL_CALL = "tool_call"
       # Future: NOTIFICATION = "notification"
   ```

9. **SQLAlchemy Enum for `interface_type`**:
   ```python
   class InterfaceType(str, Enum):
       TELEGRAM = "telegram"
       WEB = "web"
       EMAIL = "email"
   ```

#### Example Listener Configurations

```python
# "Tell me when Andrew gets home"
{
    "id": 1,  # Auto-generated
    "name": "Andrew arrival notification",
    "source_id": "home_assistant",
    "match_conditions": {
        "entity_id": "person.andrew",
        "new_state.state": "home"
    },
    "action_type": "wake_llm",
    "action_config": {
        "prompt": "Andrew just arrived home. Send a notification to the user.",
        "context": {"person": "Andrew"}
    },
    "conversation_id": "123456",  # Telegram chat ID as string
    "interface_type": "telegram",
    "one_time": true
}

# "Turn on lights when motion detected"
{
    "id": 2,
    "name": "Motion-activated lights",
    "source_id": "home_assistant",
    "match_conditions": {
        "entity_id": "sensor.hallway_motion",
        "new_state.state": "on"
    },
    "action_type": "wake_llm",
    "action_config": {
        "prompt": "Motion was detected in the hallway. Turn on the hallway lights.",
        "suggested_tool": "control_device"
    },
    "conversation_id": "123456",
    "interface_type": "telegram"
}

# "Send summary when school newsletter arrives"
{
    "id": 3,
    "name": "School newsletter summary",
    "source_id": "indexing",
    "match_conditions": {
        "document_type": "email",
        "metadata.sender": "newsletter@school.edu"
    },
    "action_type": "wake_llm",
    "action_config": {
        "prompt": "A school newsletter was just indexed. Please read it and send me a summary of important dates and announcements.",
        "include_event_data": true
    },
    "conversation_id": "123456",
    "interface_type": "telegram"
}
```

### 3. Event Processing

The event processor efficiently routes events using source and type subdivision:

```python
# src/family_assistant/events/processor.py
class EventProcessor:
    def __init__(self, sources: dict[str, EventSource], db_context: DatabaseContext):
        self.sources = sources
        self.db_context = db_context
        self.event_storage = EventStorage(db_context)
        # Cache listeners by source_id:entity_id for efficient lookup
        self._listener_cache: dict[str, list[dict]] = {}
        self._cache_refresh_interval = 60  # Refresh from DB every minute
        self._last_cache_refresh = 0
        
    async def process_event(self, source_id: str, event_data: dict[str, Any]) -> None:
        """Process an event from a source"""
        # Refresh cache if needed
        if time.time() - self._last_cache_refresh > self._cache_refresh_interval:
            await self._refresh_listener_cache()
        
        # 1. Get all active listeners for this source
        listeners = self._listener_cache.get(source_id, [])
        
        # 2. Evaluate match conditions for relevant listeners
        triggered_listener_ids = []
        for listener in listeners:
            if self._check_match_conditions(event_data, listener['match_conditions']):
                    
                # Check and update rate limit atomically
                allowed, reason = await check_and_update_rate_limit(self.db_context, listener)
                if allowed:
                    await self._execute_action(listener, event_data)
                    triggered_listener_ids.append(listener['id'])
                    
                    # Handle one-time listeners
                    if listener.get('one_time'):
                        await self._disable_listener(listener['id'])
                else:
                    logger.warning(f"Listener {listener['id']} rate limited: {reason}")
                    # Notify user about rate limiting
                    if "Daily limit exceeded" in reason:
                        await self._send_rate_limit_alert(
                            listener['conversation_id'], 
                            listener['interface_type'],
                            listener['name'], 
                            reason
                        )
        
        # 3. Store event for debugging/testing
        await self.event_storage.store_event(source_id, event_data, triggered_listener_ids)
    
    def _check_match_conditions(self, event_data: dict, match_conditions: dict | None) -> bool:
        """Check if event matches the listener's conditions using simple dict equality"""
        if not match_conditions:
            return True  # No conditions means match all events
            
        for key, expected_value in match_conditions.items():
            actual_value = self._get_nested_value(event_data, key)
            if actual_value != expected_value:
                return False
        return True
    
    def _get_nested_value(self, data: dict, key_path: str) -> Any:
        """Get value from nested dict using dot notation (e.g., 'new_state.state')"""
        keys = key_path.split('.')
        value = data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value
    
    
    async def _refresh_listener_cache(self):
        """Refresh the listener cache from database"""
        async with self.db_context.transaction():
            result = await self.db_context.execute(
                """SELECT * FROM event_listeners WHERE enabled = TRUE"""
            )
            
            new_cache = {}
            for row in result:
                listener_dict = dict(row)
                # Parse JSON fields
                listener_dict['match_conditions'] = json.loads(listener_dict.get('match_conditions') or '{}')
                listener_dict['action_config'] = json.loads(listener_dict.get('action_config') or '{}')
                
                source_id = listener_dict['source_id']
                if source_id not in new_cache:
                    new_cache[source_id] = []
                new_cache[source_id].append(listener_dict)
            
            self._listener_cache = new_cache
            self._last_cache_refresh = time.time()
            logger.info(f"Refreshed listener cache: {sum(len(v) for v in new_cache.values())} listeners across {len(new_cache)} sources")
```

Source-level filtering example for Home Assistant:
```python
class HomeAssistantSource(EventSource):
    async def _setup_subscriptions(self, listeners: list[dict]) -> None:
        # Collect all entity patterns from listeners
        entity_patterns = set()
        for listener in listeners:
            patterns = listener.get("source_config", {}).get("entity_patterns", [])
            entity_patterns.update(patterns)
        
        # Subscribe only to entities matching patterns
        await self.ha_client.subscribe_entities(entity_patterns)
```

### 4. Actions

The primary action is to wake the LLM with event context:

#### Wake LLM (`wake_llm`)
- Creates a task to wake the assistant with context
- Uses existing callback mechanism (`llm_callback` task type)
- LLM can access full conversation history and tools
- Includes event data and listener context
- Ensures LLM can't do anything via events that it couldn't do directly

Implementation in EventProcessor:
```python
async def _execute_action(
    self, 
    listener: dict[str, Any], 
    event_data: dict[str, Any]
) -> None:
    """Execute the action defined in the listener."""
    action_type = listener["action_type"]
    
    if action_type == EventActionType.wake_llm:
        # Extract configuration
        action_config = listener.get("action_config", {})
        include_event_data = action_config.get("include_event_data", True)
        
        # Prepare callback context
        callback_context = {
            "trigger": f"Event listener '{listener['name']}' matched",
            "listener_id": listener["id"],
            "source": listener["source_id"],
        }
        
        if include_event_data:
            callback_context["event_data"] = event_data
            
        # Enqueue llm_callback task
        async with get_db_context() as db_ctx:
            await enqueue_task(
                db_context=db_ctx,
                task_type="llm_callback",
                payload={
                    "context": callback_context,
                    "conversation_id": listener["conversation_id"],
                    "interface_type": listener["interface_type"],
                },
                conversation_id=listener["conversation_id"],
            )
        
        logger.info(f"Enqueued wake_llm callback for listener {listener['id']}")
```

The LLM receives the full event data and action_config, allowing flexible handling based on the specific configuration.

Future optimizations could include:
- **Direct Tool Call**: Skip LLM for deterministic actions (still validated)
- **Notification**: Template-based messages without waking LLM
- **Batching**: Combine multiple events before waking LLM

### 5. LLM Tools for Event Management

Since there's no web UI initially, ALL interaction happens through LLM tools. The LLM handles all complexity of translating user intent to technical configuration.

**Note**: Event listeners are created within a conversation context and will wake/notify in that same conversation. Listeners are isolated by conversation for security - you can only see and manage listeners from your own conversation.

```python
# Create listener (uses current conversation's ID from context)
create_event_listener(
    name: str,
    source: str,  # Must be valid EventSourceType value
    listener_config: dict,  # Contains match_conditions and optional action_config
    one_time: bool = False
) -> str  # Returns: {"success": true, "listener_id": 123, "message": "Created listener 'name'"}

# List listeners (filtered by current conversation)
list_event_listeners(
    source: str | None = None,
    enabled: bool | None = None
) -> str  # Returns list with: id, name, source, enabled, daily_executions, last_execution_at

# Delete listener (must belong to current conversation)
delete_event_listener(
    listener_id: int
) -> str  # Returns: {"success": true, "message": "Deleted listener 'name'"}

# Toggle listener enabled status (must belong to current conversation)
toggle_event_listener(
    listener_id: int,
    enabled: bool
) -> str  # Returns: {"success": true, "message": "Listener 'name' is now enabled/disabled"}

# Test listener configuration (dry run)
test_event_listener(
    source_id: str,
    match_conditions: dict,
    hours: int = 24,
    limit: int = 10  # Max events to test against
) -> str  # Returns JSON with matched_events, total_tested, and analysis

# Query recent events (for debugging)
query_recent_events(
    source_id: str | None = None,
    hours: int = 24,
    limit: int = 50
) -> str  # Returns JSON array of recent events

# Future: Explore entity events (for Home Assistant discovery)
# explore_entity_events(
#     entity_pattern: str,  # "person.*", "sensor.*temp*"
#     hours: int = 1
# ) -> list[dict]  # Shows what entities exist and their states
```

#### listener_config Structure

The `listener_config` parameter in `create_event_listener` should contain:

```python
{
    "match_conditions": {
        # Required: Dictionary of conditions to match
        "entity_id": "person.andrew",
        "new_state.state": "Home"
    },
    "action_config": {
        # Optional: Configuration for the wake_llm action
        "include_event_data": true,  # Default: true
        # Future fields can be added here
    }
}
```

#### Tool-First Example Conversation

```
User: "Tell me when Andrew gets home"

Assistant: I'll create a listener for that. Let me first test what events would match...
[calls test_event_listener(
    source_id="home_assistant",
    match_conditions={"entity_id": "person.andrew", "new_state.state": "home"},
    hours=24
)]

I see no events matched with lowercase 'home'. Let me check what events exist for Andrew...
[calls query_recent_events(
    source_id="home_assistant",
    hours=24
) and filters for person.andrew]

I found that person.andrew uses states 'Home' and 'Away' (capitalized). Let me test again with the correct state value:
[calls test_event_listener(
    source_id="home_assistant",
    match_conditions={"entity_id": "person.andrew", "new_state.state": "Home"},
    hours=24
)]

Perfect! This would have matched 3 events in the last 24 hours. Now I'll create the listener:
[calls create_event_listener(
    name="Andrew arrival notification",
    source="home_assistant",
    listener_config={
        "match_conditions": {
            "entity_id": "person.andrew",
            "new_state.state": "Home"
        }
    },
    one_time=True
)]

Created listener #1. I tested it against recent events and confirmed it will trigger when Andrew's state changes to 'Home'.

User: Why didn't it trigger yesterday?

Assistant: Let me check what actually happened yesterday...
[calls test_event_listener(
    source_id="home_assistant",
    match_conditions={"entity_id": "person.andrew", "new_state.state": "Home"},
    hours=48
)]

I see the issue - the listener was just created now, so it wasn't active yesterday. Looking at the events from yesterday, Andrew did arrive home at 6:23 PM. The listener will trigger the next time this happens.
```

## Storage Layer Functions

The following functions should be implemented in `storage/events.py` to support the CRUD tools:

```python
async def create_event_listener(
    db_context: DatabaseContext,
    name: str,
    source_id: str,
    match_conditions: dict,
    conversation_id: str,
    interface_type: str = "telegram",
    description: str | None = None,
    action_config: dict | None = None,
    one_time: bool = False,
    enabled: bool = True,
) -> int:
    """Create a new event listener, returning its ID."""
    
async def get_event_listeners(
    db_context: DatabaseContext,
    conversation_id: str,
    source_id: str | None = None,
    enabled: bool | None = None,
) -> list[dict]:
    """Get event listeners for a conversation with optional filters."""
    
async def get_event_listener_by_id(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
) -> dict | None:
    """Get a specific listener, ensuring it belongs to the conversation."""
    
async def update_event_listener_enabled(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
    enabled: bool,
) -> bool:
    """Toggle listener enabled status."""
    
async def delete_event_listener(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
) -> bool:
    """Delete a listener."""
```

## Integration Points

### Task System Integration

Event-triggered actions use the existing task queue:
- Wake LLM actions create `llm_callback` tasks
- Maintains consistency with existing infrastructure
- Benefits from task retry/failure handling
- Preserves conversation context
- The `handle_llm_callback` function in `task_worker.py` already handles the callback processing

### Event Storage (Required for Testing)

Recent events MUST be stored to enable the test_event_listener tool. Without this, users cannot debug why listeners aren't triggering.

#### Storage Strategy
- Store ALL events that trigger listeners (audit trail)
- Sample other events for testing (1 per entity per minute for Home Assistant, 1 per type per minute for other sources)
- Retain for 24-48 hours only
- This is NOT optional - it's core functionality for debugging and testing

#### Storage Implementation

```python
class EventStorage:
    def __init__(self, db_context: DatabaseContext):
        self.db_context = db_context
        self.last_stored: dict[str, float] = {}  # key -> timestamp for sampling
        
    async def store_event(
        self, 
        source_id: str, 
        event_data: dict,
        triggered_listener_ids: list[str] | None = None
    ):
        """Store event if it should be stored"""
        now = time.time()
        
        # Create sampling key based on source and entity_id if present
        entity_id = event_data.get("entity_id", "unknown")
        key = f"{source_id}:{entity_id}"
        
        # Always store if it triggered listeners
        if triggered_listener_ids:
            await self._write_event(source_id, event_data, triggered_listener_ids)
            return
            
        # Sample storage: ~1 per entity per minute
        last = self.last_stored.get(key, 0)
        if now - last > 60:  # Simple 60 second minimum
            self.last_stored[key] = now
            await self._write_event(source_id, event_data, None)
    
    async def _write_event(
        self,
        source_id: str,
        event_data: dict,
        triggered_listener_ids: list[str] | None
    ):
        """Write event to database"""
        # For Home Assistant, minimize stored data
        if source_id == "home_assistant":
            stored_data = {
                "entity_id": event_data.get("entity_id"),
                "old_state": event_data.get("old_state", {}).get("state"),
                "new_state": event_data.get("new_state", {}).get("state"),
                "last_changed": event_data.get("new_state", {}).get("last_changed"),
                # Only store key attributes
                "attributes": {
                    k: v for k, v in event_data.get("new_state", {}).get("attributes", {}).items()
                    if k in ["friendly_name", "unit_of_measurement", "device_class"]
                }
            }
        else:
            stored_data = event_data
            
        await self.db_context.execute(
            """INSERT INTO recent_events 
               (event_id, source_id, event_data, triggered_listener_ids, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            [
                f"{source_id}:{int(time.time()*1000000)}",  # Microsecond precision for uniqueness
                source_id,
                json.dumps(stored_data),
                json.dumps(triggered_listener_ids) if triggered_listener_ids else None,
                datetime.now(timezone.utc)
            ]
        )
```

#### Cleanup
```python
# Simple cleanup task running daily
async def cleanup_old_debug_events():
    # Just delete anything older than 48 hours
    await db.execute(
        "DELETE FROM recent_events WHERE created_at < ?",
        [datetime.now() - timedelta(hours=48)]
    )
```

### Web UI

Event listener management interface:
- List active listeners with status
- Create/edit listeners with natural language
- Test listeners with sample events
- View event history and debug logs
- Enable/disable listeners
- Filter by source or status

## Dictionary Matching Examples

Event filtering uses simple dictionary matching for safety and simplicity:

```python
# Motion detected
{
    "match_conditions": {
        "new_state.state": "on"
    }
}

# Motion detected after 10 PM
{
    "match_conditions": {
        "new_state.state": "on"
    },
    "time_constraints": {
        "after_hour": 22  # 10 PM
    }
}

# Email from specific sender
{
    "entity_id": "email:newsletter@school.edu",
    "match_conditions": {
        "metadata.sender": "newsletter@school.edu",
        "metadata.subject_contains": "newsletter"  # Would need LLM preprocessing
    }
}

# State change to specific value
{
    "match_conditions": {
        "new_state.state": "open",
        "old_state.state": "closed"  # Explicit check instead of inequality
    }
}

# Check attribute values
# Note: For threshold comparisons, use Home Assistant sensors or LLM in action
{
    "entity_id": "sensor.temp_above_25_and_humid_above_70",  # Binary sensor
    "match_conditions": {
        "new_state.state": "on"
    }
}
```

For complex logic like thresholds, inequalities, or string contains:
- Create binary sensors in Home Assistant for the condition
- Or let the LLM evaluate the condition when woken with full event data

## Natural Language to Dictionary Configuration

The LLM translates natural language to dictionary match conditions:

| User Says | LLM Generates JSON Config |
|-----------|---------------------------|
| "When the garage door opens" | `{"entity_id": "cover.garage_door", "match_conditions": {"new_state.state": "open"}}` |
| "If motion is detected in the backyard after 11 PM" | `{"entity_id": "sensor.backyard_motion", "match_conditions": {"new_state.state": "on"}, "time_constraints": {"after_hour": 23}}` |
| "When an email arrives from the school" | `{"entity_id": "email:*@school.edu", "match_conditions": {"document_type": "email"}}` |
| "If the temperature goes above 30 degrees" | `{"entity_id": "sensor.server_temp_high", "match_conditions": {"new_state.state": "on"}}` |

Note: For threshold conditions, the LLM should:
1. Check if a binary sensor exists (e.g., "sensor.server_temp_high")
2. If not, suggest creating one in Home Assistant
3. Or wake with all events and evaluate the condition in the action

## Extension Points

### Custom Event Sources

Plugin architecture for new sources:

```python
class CustomEventSource(EventSource):
    async def start(self) -> None:
        # Connect to external system
        # Start monitoring for events
        
    async def stop(self) -> None:
        # Cleanup connections
        
    async def emit_event(self, event: dict) -> None:
        # Send to event processor
```

Examples:
- GitHub webhook receiver
- RSS/Atom feed monitor
- MQTT subscriber
- Custom IoT devices

### Custom Actions

Extend beyond wake_llm/tool_call/notification:

```python
ACTION_HANDLERS = {
    "wake_llm": handle_wake_llm,
    "tool_call": handle_tool_call,
    "notification": handle_notification,
    "webhook": handle_webhook,  # Custom
    "script": handle_script,    # Custom
}
```

### Event Transformers

Preprocess events before filtering:

```python
class EventTransformer(Protocol):
    def transform(self, event: dict) -> dict:
        """Transform event data before condition matching"""
        ...
```

Use cases:
- Normalize event formats
- Compute derived fields
- Aggregate multiple events
- Add contextual data

## Implementation Status

### Phase 1: Home Assistant MVP ✅ COMPLETED
- ✅ Basic Home Assistant WebSocket connection (events appearing in prod)
- ✅ Event listener CRUD tools (create, list, delete, toggle) 
- ✅ Event storage in recent_events table
- ✅ Test listener tool using stored events
- ✅ Natural language to dictionary configuration in create tool
- ✅ Wake LLM action via existing callback mechanism (uses llm_callback)
- ✅ Query recent events tool for debugging
- ✅ Conversation isolation for security
- ✅ Rate limiting using DB fields (daily_executions, daily_reset_at)

### Phase 2: Production Hardening (IN PROGRESS)
- ✅ Rate limiting implemented in check_and_update_rate_limit()
- ✅ Event cleanup task scheduling (system_event_cleanup handler registered and scheduled)
- ✅ Wake LLM action execution (EventProcessor._execute_action implemented)
- ⏳ Connection retry logic for Home Assistant
- ⏳ Health check and auto-reconnect
- ⏳ Basic monitoring/alerting for connection issues

### Phase 3: Additional Sources (as needed)
- Document indexing events (if users request)
- Webhook endpoint (if users request)
- Other sources based on actual user demand

## Next Steps

### Immediate Priority: Event Cleanup Task
The most critical missing piece is the scheduled cleanup of old events from the `recent_events` table. Without this, the table will grow unbounded.

**Implementation Steps:**
1. Register the `system_event_cleanup` task handler in `task_worker.py`
2. Create a system task on startup that runs daily at 3 AM
3. Test that old events are properly cleaned up after retention period

**Code locations:**
- Handler function: Create in `task_worker.py` using existing `cleanup_old_events` from `storage/events.py`
- Task registration: Add to task handler registration in `__main__.py` or `assistant.py`
- System task creation: Add to startup sequence, possibly in `assistant.py`

### Secondary Priority: Wake LLM Action
While the rate limiting and event listener CRUD are complete, the actual action execution (waking the LLM when events match) needs to be implemented in the EventProcessor. Currently, the processor only logs when events match (see line 99-102 in `processor.py`).

**Implementation Steps:**
1. Replace the logging with a call to `_execute_action` method in `EventProcessor`
2. Implement `_execute_action` to create llm_callback tasks when listeners match
3. Test end-to-end event → listener match → LLM wake flow

**Code location:**
- `src/family_assistant/events/processor.py` lines 99-102: Replace logging with action execution

## Testing Strategy

### Unit Tests
- Dictionary matching evaluation
- Event filtering logic
- Action execution
- Event source lifecycle

### Integration Tests
- End-to-end event flow
- Source connection handling
- Task queue integration
- Error handling and recovery

### Mock Sources
- Test event source for development
- Controllable event generation
- No external dependencies
- Simulates various event patterns

### Test Scenarios
1. Create listener via natural language
2. Trigger event and verify action
3. One-time listener auto-disable
4. Multiple listeners for same event
5. Invalid match conditions format
6. Source connection failures
7. Action execution failures

## API Token Protection

Misconfigured listeners could burn through API tokens by waking the LLM too frequently. Protection strategies:

### Hybrid Rate Limiting (DB + Memory)
```python
class ListenerRateLimiter:
    def __init__(self, db_context: DatabaseContext):
        self.db_context = db_context
        # Global rate limit tracked in memory (resets on restart)
        self.global_executions: list[float] = []
        # Burst tracking in memory (short-lived, OK to lose on restart)
        self.recent_executions: dict[str, list[float]] = {}
    
    async def check_rate_limit(self, listener: dict) -> tuple[bool, str | None]:
        """Check if listener can execute. Returns (allowed, reason_if_not)"""
        now = datetime.now(timezone.utc)
        listener_id = listener['id']
        
        # 1. Check global in-memory limit
        day_ago = time.time() - 86400
        self.global_executions = [t for t in self.global_executions if t > day_ago]
        if len(self.global_executions) >= 15:
            return False, f"Global daily limit exceeded: {len(self.global_executions)} total triggers today"
        
        # 2. Check per-listener daily limit from DB
        daily_count = listener['daily_executions'] or 0
        reset_at = listener['daily_reset_at']
        
        # Reset counter if needed
        if not reset_at or now > reset_at:
            daily_count = 0
            # Will update DB after execution
        
        if daily_count >= 5:
            return False, f"Daily limit exceeded: {daily_count} triggers today"
        
        # 3. Check burst protection in memory
        recent = self.recent_executions.get(listener_id, [])
        fifteen_min_ago = time.time() - 900
        recent = [t for t in recent if t > fifteen_min_ago]
        if len(recent) >= 3:
            return False, f"Burst limit: {len(recent)} triggers in last 15 minutes"
        
        return True, None
    
    async def record_execution(self, listener_id: str):
        """Record that a listener executed (updates DB and memory)"""
        now = datetime.now(timezone.utc)
        
        # Update memory tracking
        self.global_executions.append(time.time())
        recent = self.recent_executions.get(listener_id, [])
        recent.append(time.time())
        self.recent_executions[listener_id] = recent
        
        # Update DB: increment counter or reset if new day
        await self.db_context.execute("""
            UPDATE event_listeners 
            SET daily_executions = CASE 
                    WHEN daily_reset_at IS NULL OR daily_reset_at < ? THEN 1
                    ELSE daily_executions + 1
                END,
                daily_reset_at = CASE
                    WHEN daily_reset_at IS NULL OR daily_reset_at < ? THEN ?
                    ELSE daily_reset_at
                END,
                last_execution_at = ?
            WHERE id = ?
        """, [now, now, now + timedelta(days=1), now, listener_id])
```

### Rate Limit Storage Strategy

**Hybrid Approach Rationale:**
- **Per-listener counts in DB**: Survives restarts, prevents reset of misconfigured listeners
- **Global counts in memory**: No natural DB location, acceptable to reset on restart
- **Burst tracking in memory**: Very short-lived (15 min), not worth DB writes

This approach balances persistence needs with performance:
- A misconfigured listener won't get a fresh start after a restart (daily count persists)
- Global limit resets on restart, but that's acceptable since it's just a backstop
- Burst protection resets, but 15-minute windows naturally expire quickly anyway
- DB writes only happen on actual executions, not on every rate check

### Automatic Disabling & Alerting
- Execution counts stored directly on listener row
- Send alert to user when limit is hit (uses stored chat_id)
- Daily limits reset automatically after 24 hours
- Store rate limit events in recent_events for debugging

Example alert messages:
```
⚠️ Event listener "motion_lights" hit its daily limit (5 triggers).
It won't trigger again until tomorrow. 

If this is happening frequently, the listener may be misconfigured.
Use 'test_event_listener' to debug what events it's matching.
```

```
⚠️ Global event limit reached (15 triggers today).
All event listeners are paused until tomorrow.

Consider reviewing your active listeners with 'list_event_listeners'.
```

### Testing Before Enabling
- `test_event_listener` tool does dry run without waking LLM
- Show what would happen without executing
- Recommend testing with real event samples

### Rate Limit Configuration
```yaml
event_system:
  rate_limits:
    per_listener_daily: 5      # Max triggers per listener per day
    burst_limit: 3             # Max triggers in 15 minutes
    burst_window: 900          # 15 minutes in seconds
    global_daily: 15           # Total triggers across all listeners per day
    max_active_listeners: 20   # Total listener limit
```

### Conservative Limits Rationale
- Most listeners should trigger 0-3 times per day
- 5 daily triggers allows for some edge cases
- Global limit prevents runaway token usage
- Burst protection prevents rapid firing
- These are LLM wakeups + user messages, not lightweight operations

### Simplified Rate Limiting for Hobby Project

Keep rate limiting dead simple - just use the database:

```python
async def check_and_update_rate_limit(
    db_context: DatabaseContext, 
    listener: dict
) -> tuple[bool, str | None]:
    """Check rate limit and update counter atomically"""
    now = datetime.now(timezone.utc)
    
    # Check if we need to reset daily counter
    if not listener['daily_reset_at'] or now > listener['daily_reset_at']:
        # Reset counter for new day
        tomorrow = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
        await db_context.execute(
            """UPDATE event_listeners 
               SET daily_executions = 1, 
                   daily_reset_at = ?, 
                   last_execution_at = ?
               WHERE id = ?""",
            [tomorrow, now, listener['id']]
        )
        return True, None
    
    # Check if under limit
    if listener['daily_executions'] >= 5:
        return False, f"Daily limit exceeded ({listener['daily_executions']} triggers today)"
    
    # Increment counter
    await db_context.execute(
        """UPDATE event_listeners 
           SET daily_executions = daily_executions + 1,
               last_execution_at = ?
           WHERE id = ?""",
        [now, listener['id']]
    )
    
    return True, None
```

No memory state, no complexity - just atomic DB operations.

## Security Considerations

### Dictionary Matching Safety
- Simple key-value equality checks only
- No code execution or complex expressions
- Limited to event data access only
- No file system or network access
- Minimal overhead and guaranteed termination
- Complex logic deferred to LLM in action phase

### Action Restrictions
- Tool calls validate permissions
- Rate limiting on event processing
- Audit logging of actions
- User-specific listener limits

### Source Authentication
- Home Assistant requires API token
- Webhooks use bearer tokens
- Email uses existing auth
- Sources validate SSL certificates

### Data Privacy
- Event data filtered before storage
- Sensitive fields can be excluded
- Listener configs encrypted at rest
- Access control via existing auth

## Performance Considerations

### Event Processing
- Async processing with connection pooling
- CEL expressions pre-compiled and cached
- Batch processing for high-volume sources
- Configurable rate limits per source

### Resource Management
- Connection limits per source
- Memory limits for event buffers
- Minimal CPU usage for dict matching
- Automatic backpressure handling

## Performance Considerations

### Event Processing Efficiency
- **Subdivision-based routing**: Events are indexed by (source_id, event_type) for O(1) lookup
- **Source-level filtering**: Home Assistant only sends events for subscribed entities
- **Simple dict matching**: O(n) equality checks where n is number of conditions
- **Minimal evaluation**: Only check conditions for relevant listeners

Example efficiency gain:
- Without subdivision: 1000 events/minute × 50 listeners = 50,000 condition checks
- With subdivision: 1000 events/minute × 2 relevant listeners = 2,000 condition checks

### Resource Management
- Connection limits per source
- Memory limits for event buffers
- Minimal CPU usage for dict matching
- Automatic backpressure handling

### Scalability
- Horizontal scaling via task queue
- Source connections distributed
- Database indexes on (source_id, event_type, enabled)
- Event storage partitioned by source/type

## Comparison with Existing Systems

### vs Home Assistant Automations
- **Pros**: More flexible, LLM integration, natural language config
- **Cons**: Less reliable, higher latency, requires assistant running
- **Best for**: Human-in-the-loop scenarios, complex logic

### vs Node-RED
- **Pros**: Simpler for one-off requests, no visual programming needed
- **Cons**: Less visual debugging, fewer integrations
- **Best for**: Quick automation requests, LLM-powered logic

### vs IFTTT/Zapier
- **Pros**: Self-hosted, private, custom logic, no subscription
- **Cons**: Fewer pre-built integrations, requires setup
- **Best for**: Privacy-conscious users, custom integrations

## Future Enhancements

1. **Visual Rule Builder**: Web UI for creating match conditions
2. **Event Replay**: Replay historical events for testing
3. **Listener Templates**: Pre-built listeners for common scenarios
4. **Event Correlation**: Combine multiple events into patterns
5. **Machine Learning**: Learn event patterns and suggest automations
6. **Distributed Events**: Multi-instance event processing
7. **Event Store**: Long-term event storage for analytics

## Configuration

### Environment Variables
```bash
# Event system configuration
EVENT_SYSTEM_ENABLED=true
EVENT_PROCESSOR_WORKERS=4
EVENT_RETENTION_HOURS=48
EVENT_RATE_LIMIT_DAILY=5      # Per listener daily limit
EVENT_RATE_LIMIT_GLOBAL=15    # Global daily limit

# Home Assistant source
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=your-long-lived-access-token

# Webhook source
WEBHOOK_BASE_URL=https://assistant.example.com
WEBHOOK_AUTH_TOKEN=your-webhook-token
```

### YAML Configuration
```yaml
event_system:
  enabled: true
  sources:
    home_assistant:
      url: "${HOME_ASSISTANT_URL}"
      token: "${HOME_ASSISTANT_TOKEN}"
      event_types:
        - state_changed
        - automation_triggered
    indexing:
      # Automatically integrated with document indexing pipeline
      enabled: true
    webhook:
      path: /api/events/webhook
      auth_required: true
  processing:
    workers: 4
    match_timeout: 0.1  # seconds (should never be reached with dict matching)
  storage:
    retention_hours: 48
    sample_interval_seconds: 60  # Store roughly 1 event per type per minute
    cleanup_interval_hours: 24  # Daily cleanup is sufficient
```

## System Scheduled Tasks

The event system requires periodic maintenance tasks that should run reliably regardless of restarts or configuration changes. These "system tasks" are automatically upserted on startup with fixed IDs.

### Design Principles

1. **Fixed Task IDs**: Use predictable IDs like `system_event_cleanup_daily` to ensure idempotency
2. **Upsert on Startup**: Tasks are created/updated every time the event system starts
3. **Non-user-visible**: These tasks don't appear in user-facing task lists
4. **Graceful Handling**: If a system task is already running, don't duplicate it

### Implementation Pattern

```python
# In EventProcessor.__init__ or assistant.py startup
async def setup_system_tasks(db_context: DatabaseContext):
    """Upsert system tasks on startup."""
    
    # Event cleanup task
    await storage.enqueue_task(
        db_context=db_context,
        task_id="system_event_cleanup_daily",
        task_type="system_event_cleanup",
        payload={"retention_hours": 48},
        scheduled_at=datetime.now(timezone.utc).replace(hour=3, minute=0),  # 3 AM daily
        recurrence_rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0",
        max_retries_override=5,  # Higher retry count for system tasks
    )
    
    # Future: Event compaction, statistics, etc.
```

### System Task Types

1. **Event Cleanup** (`system_event_cleanup`)
   - Deletes events older than retention period
   - Runs daily at 3 AM
   - Logs cleanup statistics

2. **Future System Tasks**:
   - Event compaction (aggregate old events)
   - Listener statistics (usage patterns)
   - Health checks (source connectivity)

### Task Handler Registration

```python
# In task worker initialization
worker.register_task_handler(
    "system_event_cleanup",
    handle_system_event_cleanup
)

async def handle_system_event_cleanup(
    exec_context: ToolExecutionContext,
    payload: dict[str, Any]
) -> None:
    """Clean up old events from the database."""
    retention_hours = payload.get("retention_hours", 48)
    
    # Use the existing cleanup_old_events function
    from family_assistant.storage.events import cleanup_old_events
    
    deleted_count = await cleanup_old_events(
        exec_context.db_context, 
        retention_hours
    )
    
    logger.info(
        f"System event cleanup completed. Deleted {deleted_count} events older than {retention_hours} hours."
    )
```

### Implementation Note

The `cleanup_old_events` function already exists in `storage/events.py` (lines 423-446) but needs to be:
1. Registered as a task handler in the task worker
2. Scheduled as a recurring system task on startup

## Conclusion

The event listener system provides a flexible, extensible way to handle automation requests that benefit from LLM intelligence and integration with the assistant's tools. By building on existing infrastructure and using safe, sandboxed filtering, it enables powerful automation scenarios while maintaining security and reliability.

The phased implementation approach allows for iterative development and testing, ensuring each component is robust before adding complexity. The system's design prioritizes ease of use through natural language configuration while keeping the matching logic simple and predictable.

## Entity Discovery and Home Assistant Integration

### The Entity Discovery Problem

Users don't know exact entity names or states. The LLM has access to Home Assistant tools to query entities, but recent events provide crucial additional context:

- **Entity names vary**: `person.andrew` vs `person.andrew_smith` vs `device_tracker.andrews_iphone`
- **State values vary**: `home`/`away` vs `Home`/`Away` vs `home`/`not_home`
- **Attributes matter**: Some entities have useful attributes beyond state

The `explore_entity_events` tool combined with Home Assistant API access gives the LLM everything needed to correctly configure listeners.

### Connection State Management

Home Assistant connections can drop. Simple strategy:

```python
class HomeAssistantSource:
    async def health_check(self):
        # Periodic ping every 30 seconds
        # On failure, attempt reconnect
        # Log but don't alert user unless extended downtime (>5 minutes)
```

For MVP, best-effort validation is sufficient:
- Create listener even if entity doesn't exist yet
- Let user test with `test_event_listener` to debug
- Home Assistant API access helps but isn't required

## Event Routing Architecture

Since there's no general event bus, keep routing simple:

### In-Memory Listener Cache
```python
class EventProcessor:
    def __init__(self):
        # Cache all active listeners grouped by (source, entity/type)
        self._listener_cache: dict[str, list[dict]] = {}
        self._cache_refresh_interval = 60  # Refresh from DB every minute
```

### Direct Integration
Each source connects directly to the event processor:
- Home Assistant WebSocket → EventProcessor
- Indexing pipeline → EventProcessor  
- No message queue or event bus needed initially

### Event Size Management

Home Assistant events can be large with many attributes. Store only what's needed:

```python
def store_home_assistant_event(event: dict) -> dict:
    # Extract only essential fields for storage
    return {
        'entity_id': event['entity_id'],
        'old_state': event['old_state']['state'],
        'new_state': event['new_state']['state'],
        'last_changed': event['new_state']['last_changed'],
        'attributes': {
            k: v for k, v in event['new_state'].get('attributes', {}).items()
            if k in ['friendly_name', 'unit_of_measurement'] or 
            k in event.get('_important_attributes', [])  # Source can hint at important attrs
        }
    }
```

### JSON Query Examples

With entity_id in match_conditions, queries become more flexible:

```sql
-- Find all listeners for a specific entity
SELECT * FROM event_listeners 
WHERE source_id = 'home_assistant' 
  AND json_extract(match_conditions, '$.entity_id') = 'person.andrew';

-- Find all listeners matching certain conditions (PostgreSQL)
SELECT * FROM event_listeners 
WHERE source_id = 'home_assistant' 
  AND match_conditions @> '{"entity_id": "person.andrew"}';

-- Find events that matched a specific entity (SQLite)
SELECT * FROM recent_events 
WHERE source_id = 'home_assistant' 
  AND json_extract(event_data, '$.entity_id') = 'sensor.hallway_motion';
```

## Key Design Principles

### Security Through Capability Parity
The event listener system follows a critical security principle: **The LLM cannot do anything via scheduled event listeners that it couldn't do directly when asked by the user**. This ensures that:

- Event listeners don't introduce new security risks
- All actions are still subject to existing permission checks
- The system remains a convenience layer, not a privilege escalation vector
- Users maintain full control over what the assistant can do

### Rate Limit Persistence Strategy
The hybrid DB/memory approach for rate limiting reflects practical tradeoffs:

- **Critical to persist**: Per-listener daily counts (prevent reset of misconfigured listeners)
- **Acceptable to lose**: Global counts and burst windows (short-lived or just backstops)
- **Performance**: DB writes only on executions, not on every rate check
- **Simplicity**: No separate rate limit table, just fields on listener row

### Efficiency Through Hierarchical Filtering
The system uses a three-stage filtering approach to minimize computational overhead:

1. **Source-level filtering**: Sources only subscribe to relevant events (e.g., specific Home Assistant entities)
2. **Entity-based indexing**: Events are routed by (source_id, entity_id) to relevant listeners
3. **Simple matching**: Only check dict equality for potentially matching listeners

This hierarchical approach ensures minimal overhead - most events are filtered out before any matching logic runs, making the system efficient even with hundreds of listeners.