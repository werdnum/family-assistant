# Starlark Integration Example for Family Assistant

This document demonstrates how starlark-pyo3 could be integrated into Family Assistant to provide secure scripting capabilities.

## Installation

Add to `pyproject.toml`:

```yaml
[project]
dependencies = [
    # ... existing dependencies
    "starlark-pyo3>=0.1.0",
    "cel-python>=0.1.5",  # For simple expressions
]

```

## Core Script Engine Implementation

### Script Engine Module

```python

# src/family_assistant/scripting/engine.py
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol
import starlark as sl
from datetime import datetime, timedelta, timezone

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.base import ToolExecutionContext

class ScriptExecutor(Protocol):
    """Protocol for script executors"""
    async def execute(self, script: str, context: Dict[str, Any]) -> Any:
        ...

@dataclass
class ScriptContext:
    """Base context provided to all scripts"""
    event: Dict[str, Any]
    time: "TimeAPI"
    db: "DatabaseReadAPI"
    state: "StateAPI"

class StarlarkExecutor:
    """Executes Starlark scripts with sandboxed context"""

    def __init__(self, db_context: DatabaseContext):
        self.db_context = db_context
        self._globals = None
        self._setup_globals()

    def _setup_globals(self):
        """Initialize Starlark globals with safe functions"""
        self._globals = sl.Globals.standard()

        # Add safe built-in functions
        self._globals["json_decode"] = json.loads
        self._globals["json_encode"] = json.dumps
        self._globals["duration"] = self._create_duration
        self._globals["time_now"] = lambda: datetime.now(timezone.utc).isoformat()

    def _create_duration(self, spec: str) -> float:
        """Create duration in seconds from spec like '5m', '1h', '30s'"""
        if spec.endswith('s'):
            return float(spec[:-1])
        elif spec.endswith('m'):
            return float(spec[:-1]) *60
        elif spec.endswith('h'):
            return float(spec[:-1]) *3600
        elif spec.endswith('d'):
            return float(spec[:-1]) *86400
        else:
            raise ValueError(f"Invalid duration spec: {spec}")

    async def execute(self, script: str, context: Dict[str, Any]) -> Any:
        """Execute a Starlark script with the given context"""
        # Create module with context
        module = sl.Module()

        # Add context APIs
        module["event"] = context.get("event", {})
        module["time"] = TimeAPI()
        module["db"] = DatabaseReadAPI(self.db_context)
        module["state"] = StateAPI()

        # Add tool execution if this is an automation context
        if "tools" in context:
            module["tools"] = SafeToolsAPI(context["tools"])

        # Parse and evaluate script
        try:
            ast = sl.parse("user_script.star", script)
            result = sl.eval(module, ast, self._globals)

            # Convert Starlark value to Python
            return self._starlark_to_python(result)
        except Exception as e:
            raise ScriptExecutionError(f"Script execution failed: {e}") from e

    def _starlark_to_python(self, value: Any) -> Any:
        """Convert Starlark values to Python equivalents"""
        # starlark-pyo3 uses JSON as intermediate format
        # This is a simplified version - real implementation would handle more types
        if hasattr(value, 'to_json'):
            return json.loads(value.to_json())
        return value

class TimeAPI:
    """Time utilities exposed to scripts"""

    @property
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    @property
    def today(self) -> datetime:
        return self.now.replace(hour=0, minute=0, second=0, microsecond=0)

    @property
    def hour(self) -> int:
        return self.now.hour

    @property
    def day_of_week(self) -> int:
        return self.now.weekday()

    def since(self, timestamp: str) -> float:
        """Seconds since the given timestamp"""
        dt = datetime.fromisoformat(timestamp)
        return (self.now - dt).total_seconds()

    def is_between(self, start_hour: int, end_hour: int) -> bool:
        """Check if current time is between given hours"""
        current = self.hour
        if start_hour <= end_hour:
            return start_hour <= current < end_hour
        else:  # Handles overnight periods
            return current >= start_hour or current < end_hour

class DatabaseReadAPI:
    """Read-only database access for scripts"""

    def __init__(self, db_context: DatabaseContext):
        self._db = db_context

    async def get_notes(self, query: Optional[str] = None, limit: int = 10) -> list:
        """Search notes with optional query"""
        async with self._db as db:
            if query:
                results = await db.notes.search(query)
                return [self._note_to_dict(n) for n in results[:limit]]
            else:
                # Get recent notes
                notes = await db.notes.get_all()  # Would need to add this method
                return [self._note_to_dict(n) for n in notes[:limit]]

    async def get_recent_events(self, event_type: Optional[str] = None, hours: int = 24) -> list:
        """Get recent events"""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with self._db as db:
            # This would need to be implemented in EventsRepository
            events = await db.events.get_since(cutoff, event_type=event_type)
            return [self._event_to_dict(e) for e in events]

    async def get_user_data(self, key: str) -> Any:
        """Get user-defined data by key"""
        async with self._db as db:
            # This could be stored in notes with special metadata
            note = await db.notes.get_by_title(f"user_data:{key}")
            if note:
                return json.loads(note.content)
            return None

    def _note_to_dict(self, note) -> dict:
        """Convert note to safe dictionary"""
        return {
            "id": note.id,
            "title": note.title,
            "content": note.content,
            "updated_at": note.updated_at.isoformat() if note.updated_at else None,
            "metadata": note.metadata or {}
        }

    def _event_to_dict(self, event) -> dict:
        """Convert event to safe dictionary"""
        return {
            "id": event.id,
            "type": event.type,
            "data": event.data,
            "timestamp": event.timestamp.isoformat()
        }

class StateAPI:
    """State management for scripts"""

    def __init__(self):
        self._state = {}

    def get(self, key: str, default: Any = None) -> Any:
        """Get state value"""
        return self._state.get(key, default)

    def set(self, key: str, value: Any):
        """Set state value"""
        self._state[key] = value

    def delete(self, key: str):
        """Delete state value"""
        self._state.pop(key, None)

class SafeToolsAPI:
    """Controlled tool execution for scripts"""

    def __init__(self, tool_provider):
        self.tool_provider = tool_provider
        self._allowed_tools = {
            "get_weather",
            "send_notification",
            "query_calendar",
            "search_notes",
            "get_lights",
            "turn_on_lights",
            "turn_off_lights"
        }

    async def execute(self, tool_name: str, **kwargs) -> Any:
        """Execute a whitelisted tool"""
        if tool_name not in self._allowed_tools:
            raise ValueError(f"Tool {tool_name} not allowed in scripts")

        # Get tool
        tool = self.tool_provider.get_tool(tool_name)
        if not tool:
            raise ValueError(f"Tool {tool_name} not found")

        # Execute with proper context
        exec_context = ToolExecutionContext(
            db_context=self.tool_provider.db_context,
            chat_interface=None,  # Scripts don't have chat access
            user_timezone="UTC"   # Would get from config
        )

        return await tool(exec_context, **kwargs)

    def is_available(self, tool_name: str) -> bool:
        """Check if tool is available for script use"""
        return tool_name in self._allowed_tools

class ScriptExecutionError(Exception):
    """Raised when script execution fails"""
    pass

```

### Event Listener Integration

```python

# src/family_assistant/events/processor.py (modified section)

from family_assistant.scripting.engine import StarlarkExecutor, ScriptExecutionError

async def _execute_action_in_context(
    self,
    listener: Dict[str, Any],
    event: EventData,
    processing_service,
    telegram_handler
) -> None:
    """Execute the action defined in the listener configuration"""
    action_type = listener.get("action", {}).get("type")
    action_config = listener.get("action", {}).get("config", {})

    if action_type == "wake_llm":
        # ... existing wake_llm implementation ...

    elif action_type == "script":
        # Execute user-defined script
        script_id = action_config.get("script_id")
        script_code = action_config.get("script")

        if not script_id and not script_code:
            logger.error(f"No script_id or script provided for listener {listener.get('name')}")
            return

        if script_id:
            # Load script from database
            async with DatabaseContext() as db:
                script_note = await db.notes.get_by_title(f"script:{script_id}")
                if not script_note:
                    logger.error(f"Script {script_id} not found")
                    return
                script_code = script_note.content

        # Execute script
        try:
            executor = StarlarkExecutor(DatabaseContext())
            context = {
                "event": {
                    "type": event.type,
                    "source": event.source,
                    "data": event.data,
                    "metadata": event.metadata
                },
                "listener": {
                    "name": listener.get("name"),
                    "conditions": listener.get("conditions", [])
                },
                "tools": processing_service.tools_provider if action_config.get("allow_tools") else None
            }

            result = await executor.execute(script_code, context)
            logger.info(f"Script executed for listener {listener.get('name')}: {result}")

        except ScriptExecutionError as e:
            logger.error(f"Script execution failed for listener {listener.get('name')}: {e}")
            # Could optionally notify user of script errors

    elif action_type == "notification":
        # Simple notification action
        template = action_config.get("template", "Event triggered: {{event.type}}")
        message = self._render_template(template, {"event": event})

        if telegram_handler:
            await telegram_handler.send_notification(message)

```

### Configuration Example

```yaml

# config.yaml
scripting:
  enabled: true

  starlark:
    max_script_size: 10000
    execution_timeout_ms: 5000
    memory_limit_mb: 50

  security:
    allowed_tools:

      - get_weather
      - send_notification
      - query_calendar
      - search_notes
      - get_lights
      - turn_on_lights
      - turn_off_lights

    rate_limits:
      executions_per_minute: 10
      tool_calls_per_execution: 5

event_listeners:

  - name: "Smart Temperature Alert"
    conditions:

      - source: home_assistant
        entity_id: sensor.outside_temperature

    # CEL pre-filter (if we also integrate CEL)
    cel_filter: "event.state > 25"

    action:
      type: script
      config:
        allow_tools: true
        script: |
          # Starlark script for smart temperature handling

          def process_temperature():
              temp = event["data"]["state"]

              # Check if it's a good time to alert
              if not time.is_between(6, 22):
                  return {"action": "skip", "reason": "Night time"}

              # Check recent alerts
              recent_events = db.get_recent_events("temp_alert", hours=1)
              if len(recent_events) > 0:
                  return {"action": "skip", "reason": "Already alerted recently"}

              # Determine severity
              if temp > 35:
                  severity = "high"
                  message = "🔥 Extreme heat warning: {}°C".format(temp)
              elif temp > 30:
                  severity = "medium"
                  message = "☀️ High temperature alert: {}°C".format(temp)
              else:
                  severity = "low"
                  message = "Temperature: {}°C".format(temp)

              # Send notification
              if severity in ["high", "medium"]:
                  tools.execute("send_notification", text=message)

                  # Log the alert
                  db.create_note(
                      title="Temperature Alert",
                      content=message,
                      metadata={"type": "temp_alert", "severity": severity}
                  )

              return {
                  "action": "notified",
                  "severity": severity,
                  "temperature": temp
              }

          # Execute the handler
          process_temperature()

```

### Script Management UI

```python

# src/family_assistant/web/routers/scripts.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import starlark as sl

from family_assistant.scripting.engine import StarlarkExecutor
from family_assistant.storage.context import DatabaseContext

router = APIRouter(prefix="/api/scripts", tags=["scripts"])

class ScriptModel(BaseModel):
    id: str
    name: str
    description: str
    code: str
    enabled: bool = True
    script_type: str = "event"  # event, automation, tool

@router.post("/validate")
async def validate_script(script: ScriptModel):
    """Validate a script for syntax errors"""
    try:
        # Parse the script
        ast = sl.parse(f"{script.id}.star", script.code)

        # Basic validation passed
        return {
            "valid": True,
            "message": "Script syntax is valid"
        }
    except Exception as e:
        return {
            "valid": False,
            "error": str(e),
            "message": "Script validation failed"
        }

@router.post("/test")
async def test_script(script: ScriptModel, test_context: dict):
    """Test a script with mock context"""
    try:
        executor = StarlarkExecutor(DatabaseContext())
        result = await executor.execute(script.code, test_context)

        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@router.get("/examples")
async def get_script_examples():
    """Get example scripts for different use cases"""
    return {
        "examples": [
            {
                "name": "Temperature Alert",
                "description": "Alert on high temperature during daytime",
                "type": "event",
                "code": TEMPERATURE_ALERT_EXAMPLE
            },
            {
                "name": "Motion Lights",
                "description": "Turn on lights with motion, auto-off after delay",
                "type": "event",
                "code": MOTION_LIGHTS_EXAMPLE
            },
            {
                "name": "Daily Summary",
                "description": "Generate daily summary of events",
                "type": "automation",
                "code": DAILY_SUMMARY_EXAMPLE
            }
        ]
    }

```

## Benefits of This Implementation

1. **Security**: Starlark provides true sandboxing - scripts cannot access filesystem, network, or system resources

2. **Performance**: Rust-based implementation with no Python GIL restrictions

3. **Familiar Syntax**: Python-like syntax reduces learning curve

4. **Type Safety**: Starlark's type system prevents many runtime errors

5. **Deterministic**: Same inputs always produce same outputs

6. **Easy Integration**: Simple pip install with binary wheels

## Next Steps

1. Implement the script engine module
2. Add script storage and management
3. Create web UI for script editing
4. Add script templates and examples
5. Implement comprehensive logging and monitoring
6. Add script sharing capabilities
