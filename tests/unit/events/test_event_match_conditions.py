"""
Unit tests for event matching logic.
"""

from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.events.processor import EventProcessor
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
        events_module._check_match_conditions(event_data, {"entity_id": "person.alex"})
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


# --- Async tests for EventProcessor._check_match_conditions AND semantics ---


@pytest.fixture()
def event_processor() -> EventProcessor:
    """Create an EventProcessor with a mocked condition evaluator."""
    processor = EventProcessor(sources={}, timezone=ZoneInfo("Australia/Sydney"))
    processor.condition_evaluator = AsyncMock()
    return processor


EVENT_DATA = {
    "entity_id": "sensor.temperature",
    "new_state": {"state": "on"},
}


@pytest.mark.asyncio
async def test_both_dict_and_script_pass(event_processor: EventProcessor) -> None:
    """When both match_conditions and condition_script pass, result is True."""
    event_processor.condition_evaluator.evaluate_condition = AsyncMock(
        return_value=True
    )
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "sensor.temperature"},
        "return True",
    )
    assert result is True
    event_processor.condition_evaluator.evaluate_condition.assert_awaited_once()


@pytest.mark.asyncio
async def test_dict_fails_script_not_evaluated(
    event_processor: EventProcessor,
) -> None:
    """When dict conditions fail, script is not evaluated (short-circuit)."""
    event_processor.condition_evaluator.evaluate_condition = AsyncMock(
        return_value=True
    )
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "wrong_entity"},
        "return True",
    )
    assert result is False
    event_processor.condition_evaluator.evaluate_condition.assert_not_awaited()


@pytest.mark.asyncio
async def test_dict_passes_script_fails(event_processor: EventProcessor) -> None:
    """When dict conditions pass but script returns False, result is False."""
    event_processor.condition_evaluator.evaluate_condition = AsyncMock(
        return_value=False
    )
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "sensor.temperature"},
        "return False",
    )
    assert result is False


@pytest.mark.asyncio
async def test_dict_only_no_script(event_processor: EventProcessor) -> None:
    """Backwards compatible: dict conditions only, no script."""
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "sensor.temperature"},
        None,
    )
    assert result is True


@pytest.mark.asyncio
async def test_dict_only_no_script_fails(event_processor: EventProcessor) -> None:
    """Dict conditions only, no script, dict fails."""
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "wrong_entity"},
        None,
    )
    assert result is False


@pytest.mark.asyncio
async def test_script_only_empty_dict(event_processor: EventProcessor) -> None:
    """Backwards compatible: script only, empty dict conditions."""
    event_processor.condition_evaluator.evaluate_condition = AsyncMock(
        return_value=True
    )
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {},
        "return True",
    )
    assert result is True
    event_processor.condition_evaluator.evaluate_condition.assert_awaited_once()


@pytest.mark.asyncio
async def test_script_only_none_dict(event_processor: EventProcessor) -> None:
    """Backwards compatible: script only, None dict conditions."""
    event_processor.condition_evaluator.evaluate_condition = AsyncMock(
        return_value=True
    )
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        None,
        "return True",
    )
    assert result is True


@pytest.mark.asyncio
async def test_no_conditions_matches_all(event_processor: EventProcessor) -> None:
    """No conditions at all matches everything."""
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        None,
        None,
    )
    assert result is True


@pytest.mark.asyncio
async def test_script_error_returns_false(event_processor: EventProcessor) -> None:
    """Script errors return False."""
    event_processor.condition_evaluator.evaluate_condition = AsyncMock(
        side_effect=Exception("script error")
    )
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "sensor.temperature"},
        "invalid script",
    )
    assert result is False
