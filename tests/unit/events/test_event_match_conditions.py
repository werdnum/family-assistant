"""
Tests for matching events against listener conditions.
"""

import ast
import json
from typing import TypedDict
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.processor import EventProcessor
from family_assistant.storage import Database
from family_assistant.storage.events import EventSourceType
from family_assistant.storage.types import (
    EventConditionEvaluatorConfig,
    MatchConditions,
)
from family_assistant.tools.events import (
    test_event_listener_tool as event_listener_test_tool,
)
from family_assistant.tools.types import ToolExecutionContext

SOURCE = EventSourceType.home_assistant.value


class ListenerTestReport(TypedDict):
    matched_events: list[dict[str, object]]
    total_tested: int
    matched_count: int
    analysis: list[str] | None


async def _test_listener_against_events(
    db_engine: AsyncEngine,
    events: list[dict[str, object]],
    match_conditions: dict[str, object],
) -> ListenerTestReport:
    """Store ``events`` and run the listener-testing tool over them."""
    db = Database(db_engine)
    for event_data in events:
        await db.events.store_event(source_id=SOURCE, event_data=event_data)

    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conversation",
        user_name="test_user",
        turn_id="test_turn",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )
    result = await event_listener_test_tool(
        exec_context, source=SOURCE, match_conditions=match_conditions
    )
    return json.loads(result)


ALEX_ARRIVES_HOME: dict[str, object] = {
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


@pytest.mark.asyncio
async def test_listener_test_matches_on_nested_dot_paths(
    db_engine: AsyncEngine,
) -> None:
    """Dot-path conditions reach nested values, and every condition must hold."""
    bob_arrives_home: dict[str, object] = {
        "entity_id": "person.bob",
        "new_state": {
            "state": "Home",
            "attributes": {"friendly_name": "Bob", "latitude": 42.0},
        },
        "old_state": "Away",
    }
    alex_arrives_elsewhere: dict[str, object] = {
        "entity_id": "person.alex",
        "new_state": {
            "state": "Away",
            "attributes": {"friendly_name": "Alex", "latitude": 42.0},
        },
        "old_state": "Away",
    }

    report = await _test_listener_against_events(
        db_engine,
        [ALEX_ARRIVES_HOME, bob_arrives_home, alex_arrives_elsewhere],
        {
            "old_state": "Away",
            "new_state.state": "Home",
            "new_state.attributes.latitude": 42.0,
            "new_state.attributes.friendly_name": "Alex",
        },
    )

    assert report["total_tested"] == 3
    assert report["matched_count"] == 1
    assert report["matched_events"][0]["event_data"] == ALEX_ARRIVES_HOME
    assert report["analysis"] is None


@pytest.mark.asyncio
async def test_listener_test_matches_nested_old_state(
    db_engine: AsyncEngine,
) -> None:
    event_data: dict[str, object] = {
        "entity_id": "person.alex",
        "old_state": {"state": "Away"},
        "new_state": {"state": "Home"},
    }
    report = await _test_listener_against_events(
        db_engine, [event_data], {"old_state.state": "Away"}
    )

    assert report["total_tested"] == 1
    assert report["matched_count"] == 1
    assert report["matched_events"][0]["event_data"] == event_data


@pytest.mark.asyncio
async def test_listener_test_explains_each_condition_the_event_fails(
    db_engine: AsyncEngine,
) -> None:
    """A non-matching event gets one explanation per failing condition."""
    report = await _test_listener_against_events(
        db_engine,
        [ALEX_ARRIVES_HOME],
        {
            "entity_id": "person.alex",
            "missing": "x",
            "new_state.missing": "x",
            "new_state.attributes.missing": "x",
            # old_state is a plain string, so there is nothing below it.
            "old_state.state": "Away",
            "new_state.state": "Away",
        },
    )

    assert report["matched_count"] == 0
    analysis = report["analysis"]
    assert analysis is not None
    assert analysis[0] == "No events matched your conditions."
    assert [line for line in analysis if line.startswith("Field ")] == [
        "Field 'missing' not found in events",
        "Field 'new_state.missing' not found in events",
        "Field 'new_state.attributes.missing' not found in events",
        "Field 'old_state.state' not found in events",
        "Field 'new_state.state' exists but has value: 'Home'",
    ]


@pytest.mark.asyncio
async def test_listener_test_shows_sample_event_structure(
    db_engine: AsyncEngine,
) -> None:
    """The mismatch analysis summarises the event's shape: types, list sizes, depth."""
    event_data: dict[str, object] = {
        "entity_id": "sensor.temperature",
        "new_state": {
            "state": "22.5",
            "attributes": {
                "unit_of_measurement": "°C",
                "temperature": 22.5,
            },
        },
        "context": {"id": "abc123", "parent_id": None},
        "list_field": [1, 2, 3],
        "empty_list": [],
        "level1": {"level2": {"level3": {"level4": "deep value"}}},
    }

    report = await _test_listener_against_events(
        db_engine, [event_data], {"entity_id": "sensor.humidity"}
    )

    analysis = report["analysis"]
    assert analysis is not None
    prefix = "Sample event structure: "
    structure_lines = [line for line in analysis if line.startswith(prefix)]
    assert len(structure_lines) == 1
    assert ast.literal_eval(structure_lines[0].removeprefix(prefix)) == {
        "entity_id": "str",
        "new_state": {
            "state": "str",
            "attributes": {"unit_of_measurement": "str", "temperature": "float"},
        },
        "context": {"id": "str", "parent_id": "NoneType"},
        "list_field": "[3 items]",
        "empty_list": "[]",
        "level1": {"level2": {"level3": "..."}},
    }


# Production gives a condition script ~100ms. None of these tests are about the
# timeout, and holding them to it under a parallel run measures machine load.
TEST_SCRIPT_TIMEOUT: EventConditionEvaluatorConfig = {
    "script_execution_timeout_ms": 5000
}


@pytest.fixture()
def event_processor() -> EventProcessor:
    return EventProcessor(
        sources={},
        timezone=ZoneInfo("Australia/Sydney"),
        config=TEST_SCRIPT_TIMEOUT,
    )


EVENT_DATA = {
    "entity_id": "sensor.temperature",
    "new_state": {"state": "on"},
}
STATE_IS_ON = "event['new_state']['state'] == 'on'"
STATE_IS_OFF = "event['new_state']['state'] == 'off'"


@pytest.mark.asyncio
async def test_both_dict_and_script_pass(event_processor: EventProcessor) -> None:
    """When both match_conditions and condition_script pass, result is True."""
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "sensor.temperature"},
        STATE_IS_ON,
    )
    assert result is True


@pytest.mark.asyncio
async def test_dict_fails_script_not_evaluated(
    event_processor: EventProcessor,
) -> None:
    """When dict conditions fail, script is not evaluated (short-circuit)."""
    evaluator = event_processor.condition_evaluator
    with patch.object(
        evaluator, "evaluate_condition", wraps=evaluator.evaluate_condition
    ) as evaluate_condition:
        result = await event_processor._check_match_conditions(
            EVENT_DATA,
            {"entity_id": "wrong_entity"},
            STATE_IS_ON,
        )
    assert result is False
    evaluate_condition.assert_not_awaited()


@pytest.mark.asyncio
async def test_dict_passes_script_fails(event_processor: EventProcessor) -> None:
    """When dict conditions pass but the script evaluates False, result is False."""
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "sensor.temperature"},
        STATE_IS_OFF,
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
@pytest.mark.parametrize("match_conditions", [{}, None], ids=["empty-dict", "none"])
@pytest.mark.parametrize(
    ("condition_script", "expected"),
    [(STATE_IS_ON, True), (STATE_IS_OFF, False)],
    ids=["script-true", "script-false"],
)
async def test_script_alone_decides_without_dict_conditions(
    event_processor: EventProcessor,
    match_conditions: MatchConditions | None,
    condition_script: str,
    expected: bool,
) -> None:
    """Backwards compatible: with no dict conditions, the script's verdict stands."""
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        match_conditions,
        condition_script,
    )
    assert result is expected


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
@pytest.mark.parametrize(
    "condition_script",
    [
        "invalid script (((",
        "event['missing']['state'] == 'on'",
        "event['new_state']['state']",
    ],
    ids=["syntax-error", "runtime-error", "non-boolean-result"],
)
async def test_script_error_returns_false(
    event_processor: EventProcessor, condition_script: str
) -> None:
    """A script that cannot produce a boolean verdict does not match."""
    result = await event_processor._check_match_conditions(
        EVENT_DATA,
        {"entity_id": "sensor.temperature"},
        condition_script,
    )
    assert result is False
