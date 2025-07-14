# Event Listener Validation System Design

## Problem Statement

The LLM frequently creates event listeners with invalid configurations that will never match:

- Invalid entity IDs (e.g., `person.teija` instead of correct format)
- Invalid states (e.g., `Chatswood` instead of `chatswood`)
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
- **Entity ID Format**: `domain.object_id` (e.g., `person.andrew`, `light.living_room`)
- **State Values**: Strings, case-sensitive

## Validation System Design

### Phase 1: Entity ID Validation (Priority: High)

#### 1.1 Entity Existence Check

- **When**: During event listener creation
- **How**: Query Home Assistant for entity existence
- **Implementation**:
  ```python
  async def validate_entity_exists(ha_client, entity_id: str) -> bool:
      """Check if entity exists in Home Assistant."""
      try:
          states = await ha_client.async_get_states()
          return any(state.entity_id == entity_id for state in states)
      except Exception:
          return False  # Fail closed - assume invalid if can't check
  ```

#### 1.2 Entity ID Format Validation

- **Pattern**: `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`
- **Rules**:
  - Must contain exactly one dot
  - Domain and object_id must start with lowercase letter
  - Can contain lowercase letters, numbers, underscores
  - No spaces or uppercase allowed

### Phase 2: State Validation (Priority: High)

#### 2.1 Person Entity State Validation

- **Valid States**:
  - Zone names from Home Assistant (fetch dynamically)
  - Special states: `home`, `not_home`, `unknown`
- **Implementation**:
  ```python
  async def get_valid_person_states(ha_client) -> set[str]:
      """Get valid states for person entities."""
      zones = await ha_client.async_get_zones()
      zone_names = {zone.name.lower() for zone in zones}
      zone_names.update({'home', 'not_home', 'unknown'})
      return zone_names
  ```

#### 2.2 Binary Sensor State Validation

- **Valid States**: `on`, `off`, `unavailable`, `unknown`

#### 2.3 Generic State Validation

- **Approach**: Query current valid states for the specific entity
- **Cache Strategy**: Cache valid states for 5 minutes per entity

### Phase 3: Advanced Validation (Priority: Medium)

#### 3.1 State History Analysis

- Analyze recent states for an entity to suggest valid values
- Warn if attempting to match a state never seen before

#### 3.2 Event Type Validation

- Ensure event_type exists in Home Assistant
- For `state_changed` events, ensure entity_id is in match_conditions

#### 3.3 Attribute Validation

- Validate nested attributes exist (e.g., `new_state.attributes.brightness`)
- Check attribute types match expected values

## Implementation Plan

### 1. Core Validation Module

Create `/workspace/src/family_assistant/events/validation.py`:

```python
class EventListenerValidator:
    def __init__(self, ha_client):
        self.ha_client = ha_client
        self._entity_cache = {}  # TTL cache
        self._zone_cache = None
        
    async def validate_match_conditions(
        self,
        match_conditions: dict,
        source_id: str
    ) -> ValidationResult:
        """Validate all match conditions."""
        
    async def validate_entity_id(self, entity_id: str) -> ValidationResult:
        """Validate entity ID format and existence."""
        
    async def validate_state_value(
        self,
        entity_id: str,
        state: str
    ) -> ValidationResult:
        """Validate state is valid for entity."""
```

### 2. Integration Points

#### 2.1 Event Listener Creation

Modify `create_event_listener_tool()` to:

1. Extract entity_id from match_conditions
2. Validate entity exists
3. Validate states are plausible
4. Return specific validation errors

#### 2.2 Test Tools Enhancement

Enhance `test_event_listener()` to:

1. Show validation warnings
2. Suggest corrections for common mistakes
3. List valid states for entities

### 3. Validation Rules by Entity Type

| Entity Domain  | Valid States                        | Validation Method    |
| -------------- | ----------------------------------- | -------------------- |
| person         | Zone names, home, not_home, unknown | Dynamic zone query   |
| light          | on, off, unavailable, unknown       | Static list          |
| switch         | on, off, unavailable, unknown       | Static list          |
| binary_sensor  | on, off, unavailable, unknown       | Static list          |
| sensor         | Any string/number                   | Recent history check |
| device_tracker | Zone names, home, not_home          | Dynamic zone query   |
| input_boolean  | on, off                             | Static list          |
| automation     | on, off                             | Static list          |
| scene          | N/A (timestamp of last activation)  | Existence check only |
| script         | on, off                             | Static list          |

### 4. Error Messages and Suggestions

#### Example Validation Response:

```json
{
  "valid": false,
  "errors": [
    {
      "field": "entity_id",
      "value": "person.teija",
      "error": "Entity does not exist",
      "suggestion": "Did you mean 'person.teja'?",
      "similar_entities": ["person.teja", "person.teresa"]
    },
    {
      "field": "new_state.state",
      "value": "Chatswood",
      "error": "Invalid state for person entity",
      "suggestion": "Use lowercase 'chatswood'",
      "valid_states": ["home", "not_home", "chatswood", "work", "unknown"]
    }
  ]
}
```

### 5. Implementation Phases

#### Phase 1: Basic Validation (Week 1)

- [ ] Create validation module
- [ ] Add entity existence check
- [ ] Add entity ID format validation
- [ ] Integrate with create_event_listener_tool

#### Phase 2: State Validation (Week 2)

- [ ] Implement person state validation
- [ ] Add binary sensor validation
- [ ] Create state suggestion system
- [ ] Add validation to test tools

#### Phase 3: Advanced Features (Week 3)

- [ ] Implement caching layer
- [ ] Add fuzzy matching for suggestions
- [ ] Create validation report tool
- [ ] Add attribute validation

#### Phase 4: LLM Integration (Week 4)

- [ ] Create pre-creation validation tool
- [ ] Add validation examples to LLM context
- [ ] Implement auto-correction suggestions
- [ ] Create validation feedback loop

## Testing Strategy

### Unit Tests

- Test each validation method independently
- Mock Home Assistant API responses
- Test edge cases (offline HA, malformed entities)

### Integration Tests

- Test full listener creation flow with validation
- Test with real Home Assistant instance
- Verify performance with caching

### LLM Testing

- Test common mistake patterns
- Verify LLM uses validation feedback
- Measure reduction in invalid listeners

## Success Metrics

1. **Validation Coverage**: 95% of event listeners validated
2. **Error Reduction**: 80% reduction in invalid listeners
3. **Performance**: \<100ms validation time (with cache)
4. **LLM Adoption**: LLM uses validation in 90% of cases

## Future Enhancements

1. **Machine Learning**: Learn common state patterns per entity
2. **Template Validation**: Validate Jinja2 templates in conditions
3. **Complex Conditions**: Support for OR, NOT, regex matching
4. **Visual Builder**: UI for creating validated listeners
5. **Dry Run Mode**: Test listener with historical events
