# Event Listener Validation - Testing Strategy Update

## Testing Approach

Following the existing test patterns in the codebase, we'll create a single functional test file that covers all validation scenarios with mocked Home Assistant dependencies.

### Test File Structure

Create `/workspace/tests/functional/test_event_listener_validation.py`:

```python
"""
Functional tests for event listener validation system.
"""

import json
from typing import Any, Dict, List, Optional, Set
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.indexing_source import IndexingSource
from family_assistant.events.processor import EventProcessor
from family_assistant.events.validation import ValidationError, ValidationResult
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.events import EventSourceType
from family_assistant.tools.event_listeners import (
    create_event_listener_tool,
    test_event_listener_tool,
)
from family_assistant.tools.types import ToolExecutionContext


class MockHomeAssistantClient:
    """Mock Home Assistant client for testing."""
    
    def __init__(self, entities: List[str], zones: List[str]):
        self.entities = {e: {"entity_id": e, "state": "unknown"} for e in entities}
        self.zones = zones
        
    async def async_get_states(self):
        """Return mock states."""
        return [
            MagicMock(entity_id=eid, state=info["state"]) 
            for eid, info in self.entities.items()
        ]
    
    async def async_get_zones(self):
        """Return mock zones."""
        return [MagicMock(name=zone) for zone in self.zones]


@pytest.fixture
async def mock_ha_source():
    """Create a mock Home Assistant source with validation."""
    # Set up entities and zones
    entities = [
        "person.alex",
        "person.bob", 
        "light.living_room",
        "sensor.temperature",
        "binary_sensor.motion",
        "switch.outlet",
    ]
    zones = ["home", "work", "northtown", "grocery_store"]
    
    # Create mock client
    mock_client = MockHomeAssistantClient(entities, zones)
    
    # Create source with mock client
    source = HomeAssistantSource(client=mock_client)
    
    # Mock the internal methods for testing
    source._entity_cache = {e: {} for e in entities}
    source._zone_cache = set(zones)
    source._cache_timestamp = float('inf')  # Never expire during tests
    
    return source


@pytest.fixture
async def mock_event_processor(mock_ha_source):
    """Create mock event processor with sources."""
    sources = {
        EventSourceType.home_assistant.value: mock_ha_source,
        EventSourceType.indexing.value: IndexingSource(),
    }
    return EventProcessor(sources=sources)


@pytest.fixture
async def exec_context_with_validation(db_engine: AsyncEngine, mock_event_processor):
    """Create execution context with validation-enabled event processor."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        # Create mock assistant with event processor
        mock_assistant = MagicMock()
        mock_assistant.event_processor = mock_event_processor
        
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="123456",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            assistant=mock_assistant,
        )
        
        yield exec_context


class TestHomeAssistantValidation:
    """Test Home Assistant source validation."""
    
    @pytest.mark.asyncio
    async def test_valid_entity_and_state(self, exec_context_with_validation):
        """Test creating listener with valid entity and state."""
        result = await create_event_listener_tool(
            exec_context=exec_context_with_validation,
            name="Valid Motion Detector",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "binary_sensor.motion",
                    "new_state.state": "on",
                }
            },
        )
        
        data = json.loads(result)
        assert data["success"] is True
        assert "listener_id" in data
    
    @pytest.mark.asyncio
    async def test_invalid_entity_id_format(self, exec_context_with_validation):
        """Test validation rejects invalid entity ID format."""
        result = await create_event_listener_tool(
            exec_context=exec_context_with_validation,
            name="Bad Format",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "Light.Living Room",  # Wrong case and space
                    "new_state.state": "on",
                }
            },
        )
        
        data = json.loads(result)
        assert data["success"] is False
        assert "validation_errors" in data
        assert any(
            e["field"] == "entity_id" and "format" in e["error"].lower()
            for e in data["validation_errors"]
        )
    
    @pytest.mark.asyncio
    async def test_nonexistent_entity(self, exec_context_with_validation):
        """Test validation rejects non-existent entity."""
        result = await create_event_listener_tool(
            exec_context=exec_context_with_validation,
            name="Ghost Entity",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "person.taylor",  # Doesn't exist
                    "new_state.state": "home",
                }
            },
        )
        
        data = json.loads(result)
        assert data["success"] is False
        assert "validation_errors" in data
        
        # Check for entity existence error
        entity_error = next(
            e for e in data["validation_errors"] 
            if e["field"] == "entity_id"
        )
        assert "does not exist" in entity_error["error"]
        assert entity_error["suggestion"] is not None  # Should suggest similar
    
    @pytest.mark.asyncio 
    async def test_invalid_person_state(self, exec_context_with_validation):
        """Test validation rejects invalid state for person entity."""
        result = await create_event_listener_tool(
            exec_context=exec_context_with_validation,
            name="Wrong Zone",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "person.alex",
                    "new_state.state": "Northtown",  # Wrong case
                }
            },
        )
        
        data = json.loads(result)
        assert data["success"] is False
        assert "validation_errors" in data
        
        # Check for state validation error
        state_error = next(
            e for e in data["validation_errors"]
            if e["field"] == "new_state.state"
        )
        assert "Invalid" in state_error["error"]
        assert "northtown" in state_error["suggestion"]  # Case correction
    
    @pytest.mark.asyncio
    async def test_binary_sensor_valid_states(self, exec_context_with_validation):
        """Test binary sensor accepts only valid states."""
        # Valid state
        result = await create_event_listener_tool(
            exec_context=exec_context_with_validation,
            name="Motion On",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "binary_sensor.motion",
                    "new_state.state": "on",
                }
            },
        )
        assert json.loads(result)["success"] is True
        
        # Invalid state
        result = await create_event_listener_tool(
            exec_context=exec_context_with_validation,
            name="Motion Active",
            source="home_assistant", 
            listener_config={
                "match_conditions": {
                    "entity_id": "binary_sensor.motion",
                    "new_state.state": "active",  # Invalid
                }
            },
        )
        data = json.loads(result)
        assert data["success"] is False
        assert any(
            "valid states" in e.get("suggestion", "").lower()
            for e in data["validation_errors"]
        )
    
    @pytest.mark.asyncio
    async def test_validation_in_test_tool(self, exec_context_with_validation):
        """Test that test_event_listener shows validation errors."""
        result = await test_event_listener_tool(
            exec_context=exec_context_with_validation,
            source="home_assistant",
            match_conditions={
                "entity_id": "person.invalid",
                "new_state.state": "InvalidZone",
            },
            hours=1,
        )
        
        data = json.loads(result)
        # Should still work but show validation in analysis
        assert "analysis" in data
        assert any("VALIDATION ISSUES" in line for line in data["analysis"])
        assert any("Entity does not exist" in line for line in data["analysis"])


class TestIndexingSourceValidation:
    """Test indexing source validation."""
    
    @pytest.mark.asyncio
    async def test_valid_indexing_event(self, exec_context_with_validation):
        """Test valid indexing event listener."""
        result = await create_event_listener_tool(
            exec_context=exec_context_with_validation,
            name="Document Ready",
            source="indexing",
            listener_config={
                "match_conditions": {
                    "event_type": "DOCUMENT_READY",
                    "document_id": "doc123",
                }
            },
        )
        
        data = json.loads(result)
        assert data["success"] is True
    
    @pytest.mark.asyncio
    async def test_invalid_event_type(self, exec_context_with_validation):
        """Test invalid indexing event type."""
        result = await create_event_listener_tool(
            exec_context=exec_context_with_validation,
            name="Bad Event",
            source="indexing",
            listener_config={
                "match_conditions": {
                    "event_type": "DOCUMENT_INDEXED",  # Invalid
                }
            },
        )
        
        data = json.loads(result)
        assert data["success"] is False
        assert any(
            e["field"] == "event_type" and "DOCUMENT_READY" in e["suggestion"]
            for e in data["validation_errors"]
        )


class TestValidationBackwardCompatibility:
    """Test that sources without validation still work."""
    
    @pytest.mark.asyncio
    async def test_source_without_validation(self, db_engine: AsyncEngine):
        """Test that sources without validate_match_conditions still work."""
        async with DatabaseContext(engine=db_engine) as db_ctx:
            # Create a minimal source without validation
            class MinimalSource:
                source_id = "minimal"
                
                async def start(self, processor):
                    pass
                
                async def stop(self):
                    pass
            
            # Create processor with minimal source
            sources = {"minimal": MinimalSource()}
            processor = EventProcessor(sources=sources)
            
            # Create context
            mock_assistant = MagicMock()
            mock_assistant.event_processor = processor
            
            exec_context = ToolExecutionContext(
                interface_type="telegram",
                conversation_id="123456",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_ctx,
                assistant=mock_assistant,
            )
            
            # Should work without validation
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="No Validation",
                source="minimal",
                listener_config={
                    "match_conditions": {"any": "value"}
                },
            )
            
            data = json.loads(result)
            assert data["success"] is True


class TestValidationPerformance:
    """Test validation caching and performance."""
    
    @pytest.mark.asyncio
    async def test_validation_caching(self, mock_ha_source):
        """Test that validation uses caching effectively."""
        # Spy on the API call
        with patch.object(
            mock_ha_source.client, 
            'async_get_states',
            wraps=mock_ha_source.client.async_get_states
        ) as mock_get_states:
            
            # First validation - should hit API
            result1 = await mock_ha_source.validate_match_conditions({
                "entity_id": "person.alex",
                "new_state.state": "home",
            })
            assert result1.valid
            assert mock_get_states.call_count == 1
            
            # Second validation - should use cache
            result2 = await mock_ha_source.validate_match_conditions({
                "entity_id": "person.bob",
                "new_state.state": "work",
            })
            assert result2.valid
            assert mock_get_states.call_count == 1  # Still 1, used cache
```

### Key Testing Patterns

1. **Mock Home Assistant Client**
   - Create `MockHomeAssistantClient` class with predefined entities and zones
   - Mock `async_get_states()` and `async_get_zones()` methods
   - Control exactly which entities exist for testing

2. **Fixture Hierarchy**
   - `mock_ha_source` - Home Assistant source with mocked client
   - `mock_event_processor` - Event processor with all sources
   - `exec_context_with_validation` - Full execution context

3. **Test Organization**
   - Group by source type (TestHomeAssistantValidation, TestIndexingSourceValidation)
   - Test both success and failure cases
   - Test backward compatibility
   - Test performance/caching

4. **Assertion Patterns**
   - Parse JSON responses and check structure
   - Look for specific error fields and messages
   - Verify suggestions are provided
   - Check that validation doesn't break existing functionality

### Integration with CI/CD

Add to `poe test` or create specific validation test command:

```toml
[tool.poe.tasks]
test-validation = "pytest tests/functional/test_event_listener_validation.py -v"
```

### Mock Data Sets

For comprehensive testing, we'll maintain these mock data sets:

```python
# Standard test entities
TEST_ENTITIES = [
    "person.alex",
    "person.bob",
    "light.living_room",
    "light.bedroom", 
    "sensor.temperature",
    "sensor.humidity",
    "binary_sensor.motion",
    "binary_sensor.door",
    "switch.outlet",
    "automation.morning_routine",
]

# Standard test zones  
TEST_ZONES = [
    "home",
    "work", 
    "northtown",
    "grocery_store",
    "gym",
]

# Common validation test cases
VALIDATION_TEST_CASES = [
    # (entity_id, state, should_be_valid, expected_error)
    ("person.alex", "home", True, None),
    ("person.alex", "Home", False, "case-sensitive"),
    ("person.taylor", "home", False, "does not exist"),
    ("Person.Alex", "home", False, "Invalid entity ID format"),
    ("binary_sensor.motion", "on", True, None),
    ("binary_sensor.motion", "active", False, "Invalid state"),
]
```

This testing approach ensures comprehensive coverage while following the established patterns in the codebase.