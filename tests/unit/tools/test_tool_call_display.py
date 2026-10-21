"""Tests for the phrases clients show for a tool call."""

import json

import pytest

from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.tools.catalog import TOOLS_DEFINITION
from family_assistant.tools.display import describe_tool_call, has_phrase
from family_assistant.web.tool_call_serialization import serialize_tool_call


def test_phrase_names_the_key_argument_in_both_tenses() -> None:
    display = describe_tool_call("search_documents", '{"query": "dentist"}')

    assert display == {
        "active": "Searching documents for “dentist”",
        "done": "Searched documents for “dentist”",
        "changes": False,
    }


def test_state_changing_tool_is_marked() -> None:
    display = describe_tool_call(
        "add_calendar_event",
        {"summary": "Swimming", "start_time": "2026-10-15T09:00"},
    )

    assert display["done"] == "Added event “Swimming”"
    assert display["changes"] is True


def test_missing_argument_leaves_the_bare_verb() -> None:
    assert describe_tool_call("get_note", "{}")["active"] == "Reading note"


@pytest.mark.parametrize("arguments", ["not json", "[1, 2]", ""])
def test_unparseable_arguments_do_not_fail(arguments: str) -> None:
    assert describe_tool_call("get_note", arguments)["done"] == "Read note"


def test_composite_detail() -> None:
    display = describe_tool_call(
        "call_home_assistant_action",
        {"domain": "light", "action": "turn_on", "service_data": {}},
    )

    assert display["active"] == "Running light.turn_on"


def test_calendar_range_reads_as_dates() -> None:
    display = describe_tool_call(
        "search_calendar_events",
        {"start_date": "2026-10-14", "end_date": "2026-10-20"},
    )

    assert display["done"] == "Checked calendar from 2026-10-14 to 2026-10-20"


def test_unknown_tool_falls_back_to_name_and_primary_argument() -> None:
    display = describe_tool_call("frobnicate_widgets", {"name": "Sprocket", "n": 3})

    assert display == {
        "active": "Frobnicate widgets · Sprocket",
        "done": "Frobnicate widgets · Sprocket",
        "changes": False,
    }


def test_argument_text_is_single_line_and_truncated() -> None:
    query = "line one\nline two " + "x" * 200

    phrase = describe_tool_call("search_documents", {"query": query})["active"]

    assert "\n" not in phrase
    assert phrase.endswith("…”")
    assert len(phrase) < 100


def test_household_tools_have_written_phrases() -> None:
    """The tools a household turn routinely makes read as phrases, not names."""
    names = {tool["function"]["name"] for tool in TOOLS_DEFINITION}
    household = {
        "get_note",
        "add_or_update_note",
        "search_documents",
        "search_calendar_events",
        "add_calendar_event",
        "schedule_reminder",
        "delegate_to_service",
        "call_home_assistant_action",
        "send_message_to_user",
    }

    assert household <= names
    assert all(has_phrase(name) for name in household)


def test_serialized_tool_call_carries_display() -> None:
    tool_call = ToolCallItem(
        id="call_1",
        type="function",
        function=ToolCallFunction(name="get_note", arguments={"title": "Groceries"}),
    )

    serialized = serialize_tool_call(tool_call)

    assert json.loads(serialized["function"]["arguments"]) == {"title": "Groceries"}
    assert serialized["display"]["active"] == "Reading note “Groceries”"
