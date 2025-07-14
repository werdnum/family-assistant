# Event Listener Validation System Design

## Problem Statement

The LLM frequently creates event listeners with invalid configurations that will never match:

- Invalid entity IDs (e.g., `person.taylor` instead of correct format)
- Invalid states (e.g., `Northtown` instead of `northtown`)
- Entity IDs that don't exist in Home Assistant
- States that are not valid for the given entity type

## Current State Analysis

### Event Flow

1. Event listeners are created with `match_conditions` (JSONB)
2. Events from Home Assistant are processed in `EventProcessor`
3. Match conditions use simple equality checks with dot notation
4. No validation occurs during listener creation

### Key Components

- **Event Sources**: Home Assistant, indexing, webhook
- **Match Conditions**: Simple dict with dot notation support (e.g., `new_state.state`)
- **Entity ID Format**: `domain.object_id` (e.g., `person.alex`, `light.living_room`)
- **State Values**: Strings, case-sensitive

## Architecture Design

### Source-Specific Validation

Each event source will implement its own validation logic, as validation rules are inherently
source-specific.

#### Protocol Extension

```python
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

@dataclass
class ValidationError:
    field: str
    value: Any
    error: str
    suggestion: Optional[str] = None
    similar_values: Optional[List[str]] = None

@dataclass
class ValidationResult:
    valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": [
                {
                    "field": e.field,
                    "value": e.value,
                    "error": e.error,
                    "suggestion": e.suggestion,
                    "similar_values": e.similar_values
                }
                for e in self.errors
            ],
            "warnings": self.warnings
        }

class EventSource(Protocol):
    """Extended event source protocol with validation."""
    
    async def validate_match_conditions(
        self, match_conditions: Dict[str, Any]
    ) -> ValidationResult:
        """
        Validate match conditions for this source type.
        
        Default implementation returns valid=True for backward compatibility.
        """
        return ValidationResult(valid=True)
```

### Source Implementations

#### Home Assistant Source Validation

```python
class HomeAssistantSource(EventSource):
    """Home Assistant event source with comprehensive validation."""
    
    def __init__(self, client: ha_api.Client, ...):
        self.client = client
        self._entity_cache: Dict[str, EntityInfo] = {}
        self._zone_cache: Optional[Set[str]] = None
        self._cache_timestamp = 0
        self._cache_ttl = 300  # 5 minutes
        
    async def validate_match_conditions(
        self, match_conditions: Dict[str, Any]
    ) -> ValidationResult:
        """Validate Home Assistant specific conditions."""
        errors = []
        warnings = []
        
        # Refresh cache if needed
        await self._refresh_cache_if_needed()
        
        # Extract and validate entity_id
        entity_id = None
        for field in ["entity_id", "data.entity_id"]:
            if field in match_conditions:
                entity_id = match_conditions[field]
                validation = await self._validate_entity_id(entity_id, field)
                if not validation.valid:
                    errors.extend(validation.errors)
                break
        
        # Validate state values
        state_fields = [
            ("new_state.state", "new state"),
            ("old_state.state", "old state"),
            ("state", "state"),
            ("to_state.state", "to state"),
            ("from_state.state", "from state")
        ]
        
        for field, description in state_fields:
            if field in match_conditions:
                state_value = match_conditions[field]
                if entity_id:
                    validation = await self._validate_state_for_entity(
                        entity_id, state_value, field, description
                    )
                    if not validation.valid:
                        errors.extend(validation.errors)
                else:
                    warnings.append(
                        f"Cannot validate {description} without entity_id"
                    )
        
        # Validate attributes
        attribute_fields = self._extract_attribute_fields(match_conditions)
        for field, attr_name in attribute_fields:
            validation = await self._validate_attribute(
                entity_id, attr_name, match_conditions[field], field
            )
            if not validation.valid:
                errors.extend(validation.errors)
        
        # Event type specific validation
        if "event_type" in match_conditions:
            event_type = match_conditions["event_type"]
            if event_type == "state_changed" and not entity_id:
                errors.append(ValidationError(
                    field="entity_id",
                    value=None,
                    error="state_changed events require entity_id",
                    suggestion="Add entity_id to match_conditions"
                ))
        
        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings
        )
    
    async def _validate_entity_id(
        self, entity_id: str, field: str
    ) -> ValidationResult:
        """Validate entity ID format and existence."""
        errors = []
        
        # Format validation
        if not self._is_valid_entity_format(entity_id):
            errors.append(ValidationError(
                field=field,
                value=entity_id,
                error="Invalid entity ID format",
                suggestion="Format: domain.object_id (lowercase, no spaces)"
            ))
            return ValidationResult(valid=False, errors=errors)
        
        # Existence check
        if entity_id not in self._entity_cache:
            similar = self._find_similar_entities(entity_id)
            errors.append(ValidationError(
                field=field,
                value=entity_id,
                error="Entity does not exist",
                suggestion=f"Did you mean '{similar[0]}'?" if similar else None,
                similar_values=similar[:5]
            ))
        
        return ValidationResult(valid=len(errors) == 0, errors=errors)
    
    async def _validate_state_for_entity(
        self, entity_id: str, state: str, field: str, description: str
    ) -> ValidationResult:
        """Validate state value for specific entity type."""
        errors = []
        domain = entity_id.split(".")[0]
        
        # Get valid states based on domain
        valid_states = await self._get_valid_states_for_domain(domain, entity_id)
        
        if valid_states is not None and state not in valid_states:
            # Try case-insensitive match for suggestion
            lower_state = state.lower()
            case_matches = [s for s in valid_states if s.lower() == lower_state]
            
            suggestion = None
            if case_matches:
                suggestion = f"Use '{case_matches[0]}' (case-sensitive)"
            else:
                # Find similar states
                similar = self._find_similar_strings(state, list(valid_states))
                if similar:
                    suggestion = f"Did you mean '{similar[0]}'?"
            
            errors.append(ValidationError(
                field=field,
                value=state,
                error=f"Invalid {description} for {domain} entity",
                suggestion=suggestion,
                similar_values=list(valid_states)[:10]  # Limit size
            ))
        
        return ValidationResult(valid=len(errors) == 0, errors=errors)
```

#### Indexing Source Validation

```python
class IndexingSource(EventSource):
    """Indexing event source with validation."""
    
    VALID_EVENT_TYPES = {"DOCUMENT_READY", "INDEXING_FAILED"}
    
    async def validate_match_conditions(
        self, match_conditions: Dict[str, Any]
    ) -> ValidationResult:
        """Validate indexing event conditions."""
        errors = []
        
        # Validate event_type
        if "event_type" in match_conditions:
            event_type = match_conditions["event_type"]
            if event_type not in self.VALID_EVENT_TYPES:
                errors.append(ValidationError(
                    field="event_type",
                    value=event_type,
                    error=f"Invalid event type for indexing source",
                    suggestion=f"Must be one of: {', '.join(self.VALID_EVENT_TYPES)}"
                ))
        
        # Validate document_id format if present
        if "document_id" in match_conditions:
            doc_id = match_conditions["document_id"]
            if not isinstance(doc_id, str) or not doc_id:
                errors.append(ValidationError(
                    field="document_id",
                    value=doc_id,
                    error="document_id must be a non-empty string"
                ))
        
        # Validate interface_type if present
        if "interface_type" in match_conditions:
            interface = match_conditions["interface_type"]
            valid_interfaces = {"api", "discord"}
            if interface not in valid_interfaces:
                errors.append(ValidationError(
                    field="interface_type",
                    value=interface,
                    error="Invalid interface type",
                    suggestion=f"Must be one of: {', '.join(valid_interfaces)}"
                ))
        
        return ValidationResult(valid=len(errors) == 0, errors=errors)
```

#### Webhook Source Validation

```python
class WebhookSource(EventSource):
    """Webhook event source with validation."""
    
    def __init__(self, webhook_registry: WebhookRegistry):
        self.webhook_registry = webhook_registry
    
    async def validate_match_conditions(
        self, match_conditions: Dict[str, Any]
    ) -> ValidationResult:
        """Validate webhook event conditions."""
        errors = []
        
        # Validate webhook_id
        if "webhook_id" in match_conditions:
            webhook_id = match_conditions["webhook_id"]
            if not await self.webhook_registry.exists(webhook_id):
                registered = await self.webhook_registry.list_webhooks()
                errors.append(ValidationError(
                    field="webhook_id",
                    value=webhook_id,
                    error="Webhook ID not registered",
                    suggestion="Register webhook first or use existing ID",
                    similar_values=[w.id for w in registered]
                ))
        
        # Validate payload structure expectations
        payload_fields = [k for k in match_conditions.keys() 
                         if k.startswith("payload.")]
        if payload_fields and "webhook_id" in match_conditions:
            webhook = await self.webhook_registry.get(match_conditions["webhook_id"])
            if webhook and webhook.schema:
                for field in payload_fields:
                    path = field.split(".")[1:]  # Remove 'payload.' prefix
                    if not self._path_exists_in_schema(path, webhook.schema):
                        errors.append(ValidationError(
                            field=field,
                            value=match_conditions[field],
                            error="Field not defined in webhook schema"
                        ))
        
        return ValidationResult(valid=len(errors) == 0, errors=errors)
```

### Tool Integration

#### Enhanced create_event_listener_tool

```python
async def create_event_listener_tool(
    exec_context: ToolExecutionContext,
    name: str,
    source: str,
    listener_config: dict[str, Any],
    action_type: str = "wake_llm",
    script_code: str | None = None,
    script_config: dict[str, Any] | None = None,
    one_time: bool = False,
) -> str:
    """Create a new event listener with validation."""
    try:
        # Get the event source
        event_processor = exec_context.assistant.event_processor
        event_source = event_processor.sources.get(source)
        
        if not event_source:
            return json.dumps({
                "success": False,
                "message": f"Event source '{source}' not found"
            })
        
        # Extract match conditions
        match_conditions = listener_config.get("match_conditions", {})
        
        # Validate match conditions if source supports it
        if hasattr(event_source, 'validate_match_conditions'):
            validation = await event_source.validate_match_conditions(match_conditions)
            
            if not validation.valid:
                return json.dumps({
                    "success": False,
                    "message": "Validation failed",
                    "validation_errors": [
                        {
                            "field": e.field,
                            "value": e.value,
                            "error": e.error,
                            "suggestion": e.suggestion,
                            "similar_values": e.similar_values
                        }
                        for e in validation.errors
                    ],
                    "warnings": validation.warnings
                })
            
            # Log warnings but allow creation
            if validation.warnings:
                logger.warning(
                    f"Validation warnings for listener '{name}': "
                    f"{', '.join(validation.warnings)}"
                )
        
        # Continue with existing creation logic...
```

#### Enhanced test_event_listener_tool

```python
async def test_event_listener_tool(
    exec_context: ToolExecutionContext,
    source: str,
    match_conditions: dict[str, Any],
    hours: int = 24,
    limit: int = 10,
) -> str:
    """Test event listener with validation feedback."""
    # ... existing code ...
    
    # After checking matches, add validation info to analysis
    if total_tested > 0 and len(matched_events) == 0:
        # Get validation results
        event_processor = exec_context.assistant.event_processor
        event_source = event_processor.sources.get(source)
        
        if event_source and hasattr(event_source, 'validate_match_conditions'):
            validation = await event_source.validate_match_conditions(match_conditions)
            
            if not validation.valid:
                analysis.append("VALIDATION ISSUES FOUND:")
                for error in validation.errors:
                    analysis.append(f"- {error.field}: {error.error}")
                    if error.suggestion:
                        analysis.append(f"  Suggestion: {error.suggestion}")
                    if error.similar_values and len(error.similar_values) <= 5:
                        analysis.append(f"  Valid values: {error.similar_values}")
            
            if validation.warnings:
                analysis.append("WARNINGS:")
                for warning in validation.warnings:
                    analysis.append(f"- {warning}")
```

## Implementation Plan

### Phase 1: Core Infrastructure (Days 1-3)

1. **Day 1: Protocol and Base Classes**

   - [ ] Add `validate_match_conditions` to EventSource protocol
   - [ ] Create ValidationResult and ValidationError dataclasses
   - [ ] Add default implementation (returns valid=True)
   - [ ] Write unit tests for data structures

2. **Day 2: Tool Integration**

   - [ ] Modify create_event_listener_tool to call validation
   - [ ] Update test_event_listener_tool to show validation in analysis
   - [ ] Add validation error formatting helpers
   - [ ] Update tool response documentation

3. **Day 3: Testing Framework**

   - [ ] Create validation test fixtures
   - [ ] Add integration tests for tool validation flow
   - [ ] Test backward compatibility (sources without validation)
   - [ ] Performance benchmarks for validation

### Phase 2: Home Assistant Validation (Days 4-8)

4. **Day 4: Entity Validation**

   - [ ] Implement entity format validation (regex)
   - [ ] Add entity existence checking via HA API
   - [ ] Implement entity cache with TTL
   - [ ] Create fuzzy matching for entity suggestions

5. **Day 5: State Validation - Basic**

   - [ ] Implement state validation for binary domains
   - [ ] Add person/device_tracker zone validation
   - [ ] Query and cache Home Assistant zones
   - [ ] Handle case-sensitivity issues

6. **Day 6: State Validation - Advanced**

   - [ ] Add historical state analysis
   - [ ] Implement state validation for numeric sensors
   - [ ] Add attribute validation support
   - [ ] Create suggestion engine for states

7. **Day 7: Caching and Performance**

   - [ ] Implement efficient caching layer
   - [ ] Add cache warming on startup
   - [ ] Optimize API calls with batching
   - [ ] Add cache metrics and logging

8. **Day 8: Home Assistant Testing**

   - [ ] Unit tests with mocked HA client
   - [ ] Integration tests with test HA instance
   - [ ] Performance tests with large entity counts
   - [ ] Edge case testing (offline HA, etc.)

### Phase 3: Other Sources (Days 9-11)

09. **Day 9: Indexing Source**

    - [ ] Implement event type validation
    - [ ] Add document_id format validation
    - [ ] Validate interface_type values
    - [ ] Write comprehensive tests

10. **Day 10: Webhook Source**

    - [ ] Implement webhook_id validation
    - [ ] Add webhook registry integration
    - [ ] Validate payload schema paths
    - [ ] Create webhook-specific tests

11. **Day 11: Integration Testing**

    - [ ] End-to-end validation testing
    - [ ] Multi-source validation scenarios
    - [ ] Performance testing across sources
    - [ ] Documentation updates

### Phase 4: LLM Integration (Days 12-14)

12. **Day 12: LLM Context Enhancement**

    - [ ] Add validation examples to CLAUDE.md
    - [ ] Create validation best practices guide
    - [ ] Update tool descriptions with validation info
    - [ ] Add common error patterns to context

13. **Day 13: Feedback Loop**

    - [ ] Implement validation metrics collection
    - [ ] Create validation success tracking
    - [ ] Add telemetry for common errors
    - [ ] Build error pattern analysis

14. **Day 14: Polish and Release**

    - [ ] Final testing and bug fixes
    - [ ] Performance optimization
    - [ ] Documentation completion
    - [ ] Release notes and migration guide

## Testing Strategy

We'll create a single comprehensive functional test file
(`tests/functional/test_event_listener_validation.py`) that follows the existing test patterns in
the codebase, with mocked Home Assistant dependencies.

### Key Testing Components

1. **Mock Home Assistant Client**

   - `MockHomeAssistantClient` class with predefined entities and zones
   - Controls exactly which entities exist for testing scenarios
   - Mocks API methods like `async_get_states()` and `async_get_zones()`

2. **Test Organization**

   - Grouped by source type (Home Assistant, Indexing, Webhook)
   - Tests both success and failure cases
   - Includes backward compatibility tests
   - Performance and caching tests

3. **Mock Data Sets**

   ```python
   TEST_ENTITIES = ["person.alex", "person.bob", "light.living_room", ...]
   TEST_ZONES = ["home", "work", "northtown", "grocery_store"]
   ```

### Example Test Structure

```python
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
```

For the complete testing implementation, see
[Event Listener Validation - Testing Strategy](./event-listener-validation-testing.md).

## Success Metrics

1. **Validation Coverage**:

   - 100% of event sources implement validation
   - 95% of created listeners pass validation

2. **Error Reduction**:

   - 80% reduction in "never matching" listeners
   - 90% reduction in case-sensitivity errors

3. **Performance**:

   - \<50ms validation time with warm cache
   - \<200ms validation time with cold cache
   - \<1% overhead on event processing

4. **LLM Adoption**:

   - LLM successfully uses validation feedback in 95% of retry attempts
   - 70% reduction in validation-related user complaints

## Migration Guide

### For Existing Event Sources

1. Validation is optional - sources without the method continue to work
2. Add validation incrementally - start with most common errors
3. Use warnings for non-critical issues to avoid breaking changes

### For Tool Users

1. Existing tools continue to work - validation is additive
2. New error responses include structured validation data
3. test_event_listener now shows validation issues in analysis

## Future Enhancements

1. **Machine Learning**:

   - Learn common state patterns per entity
   - Predict likely states based on entity name
   - Auto-suggest corrections based on history

2. **Advanced Validation**:

   - Template validation for Jinja2 conditions
   - Complex condition support (OR, NOT, regex)
   - Time-based validation (e.g., "sun.sun" states)

3. **Developer Experience**:

   - Visual validation UI
   - Real-time validation in web interface
   - Validation playground for testing

4. **Smart Suggestions**:

   - Context-aware suggestions based on other listeners
   - Common pattern library
   - Auto-fix for simple errors
