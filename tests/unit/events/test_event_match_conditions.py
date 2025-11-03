"""
Unit tests for event matching logic.
"""

from family_assistant.tools import events as events_module


def test_get_nested_value() -> None:
    """Test getting nested values from dicts."""
    data = {
        "entity_id": "person.alex",
        "new_state": {
            "state": "Home",
            "attributes": {
                "friendly_name": "Alex",
                "latitude": 42.0,
            },
        },
        "old_state": "Away",
    }

    # Test basic access
    assert events_module._get_nested_value(data, "entity_id") == "person.alex"
    assert events_module._get_nested_value(data, "old_state") == "Away"

    # Test nested access
    assert events_module._get_nested_value(data, "new_state.state") == "Home"
    assert (
        events_module._get_nested_value(data, "new_state.attributes.friendly_name")
        == "Alex"
    )
    assert (
        events_module._get_nested_value(data, "new_state.attributes.latitude") == 42.0
    )

    # Test non-existent keys
    assert events_module._get_nested_value(data, "missing") is None
    assert events_module._get_nested_value(data, "new_state.missing") is None
    assert events_module._get_nested_value(data, "new_state.attributes.missing") is None

    # Test invalid paths
    assert (
        events_module._get_nested_value(data, "old_state.state") is None
    )  # old_state is a string


def test_check_match_conditions() -> None:
    """Test event matching logic."""
    event_data = {
        "entity_id": "person.alex",
        "new_state": {"state": "Home"},
        "old_state": {"state": "Away"},
    }

    # Test exact matches
    assert (
        events_module._check_match_conditions(
            event_data, {"entity_id": "person.alex"}
        )
        is True
    )
    assert (
        events_module._check_match_conditions(event_data, {"new_state.state": "Home"})
        is True
    )
    assert (
        events_module._check_match_conditions(event_data, {"old_state.state": "Away"})
        is True
    )

    # Test multiple conditions (AND logic)
    assert (
        events_module._check_match_conditions(
            event_data,
            {
                "entity_id": "person.alex",
                "new_state.state": "Home",
            },
        )
        is True
    )

    # Test non-matches
    assert (
        events_module._check_match_conditions(event_data, {"entity_id": "person.bob"})
        is False
    )
    assert (
        events_module._check_match_conditions(event_data, {"new_state.state": "Away"})
        is False
    )

    # Test partial match with multiple conditions
    assert (
        events_module._check_match_conditions(
            event_data,
            {
                "entity_id": "person.alex",  # matches
                "new_state.state": "Away",  # doesn't match
            },
        )
        is False
    )

    # Test empty conditions (matches all)
    assert events_module._check_match_conditions(event_data, {}) is True
    assert events_module._check_match_conditions(event_data, None) is True


def test_get_event_structure() -> None:
    """Test event structure extraction."""
    event_data = {
        "entity_id": "sensor.temperature",
        "new_state": {
            "state": "22.5",
            "attributes": {
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "friendly_name": "Living Room Temperature",
            },
            "last_changed": "2025-01-01T10:00:00Z",
        },
        "old_state": {
            "state": "22.0",
            "attributes": {
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "friendly_name": "Living Room Temperature",
            },
            "last_changed": "2025-01-01T09:00:00Z",
        },
        "context": {
            "id": "abc123",
            "parent_id": None,
            "user_id": None,
        },
        "list_field": [1, 2, 3],
        "empty_list": [],
    }

    structure = events_module._get_event_structure(event_data)

    # Check top-level structure
    assert isinstance(structure, dict)
    assert structure["entity_id"] == "str"  # type: ignore[index]
    assert structure["list_field"] == "[3 items]"  # type: ignore[index]
    assert structure["empty_list"] == "[]"  # type: ignore[index]

    # Check nested structure
    assert isinstance(structure["new_state"], dict)  # type: ignore[index]
    assert structure["new_state"]["state"] == "str"  # type: ignore[index]
    assert isinstance(structure["new_state"]["attributes"], dict)  # type: ignore[index]
    assert structure["new_state"]["attributes"]["unit_of_measurement"] == "str"  # type: ignore[index]

    # Test max depth limiting
    deep_data = {"level1": {"level2": {"level3": {"level4": {"level5": "deep value"}}}}}

    structure = events_module._get_event_structure(deep_data, max_depth=3)
    assert isinstance(structure, dict)
    assert structure["level1"]["level2"]["level3"] == "..."  # type: ignore[index]
