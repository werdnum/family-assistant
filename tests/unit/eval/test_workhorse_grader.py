"""The workhorse grader: an agent that inspects state with the household's read tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from family_assistant.eval.workhorse.grader import (
    READ_TOOL_PATTERN,
    TrialEvidence,
    grade_trial,
)
from family_assistant.llm import LLMOutput
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from tests.mocks.mock_llm import MatcherArgs

pytestmark = pytest.mark.no_db


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object"},
        },
    }


@pytest.mark.parametrize(
    ("name", "offered"),
    [
        ("search_calendar_events", True),
        ("get_note", True),
        ("tuit_find_tasks", True),
        ("render_home_assistant_template", True),
        ("ha_call_read_tool", True),
        ("add_or_update_note", False),
        ("delete_calendar_event", False),
        ("tuit_checkpoint", False),
        ("call_home_assistant_action", False),
        ("ha_call_write_tool", False),
        ("send_message_to_user", False),
    ],
)
def test_only_read_tools_are_offered(name: str, offered: bool) -> None:
    assert (READ_TOOL_PATTERN.match(name) is not None) is offered


def _call(call_id: str, name: str, arguments: dict[str, object]) -> LLMOutput:
    return LLMOutput(
        tool_calls=[
            ToolCallItem(
                id=call_id,
                type="function",
                function=ToolCallFunction(name=name, arguments=json.dumps(arguments)),
            )
        ]
    )


async def test_the_grader_reads_state_then_submits_a_verdict() -> None:
    offered: list[list[str]] = []
    executed: list[str] = []

    def sandbox(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tools/definitions":
            names = ["get_note", "add_or_update_note"]
            return httpx.Response(200, json={"tools": [_definition(n) for n in names]})
        executed.append(request.url.path)
        return httpx.Response(200, json={"result": {"content": "v1\nflights"}})

    def respond(args: MatcherArgs) -> LLMOutput:
        offered.append([t["function"]["name"] for t in args["tools"]])
        if not any(m.role == "tool" for m in args["messages"]):
            return _call("c1", "get_note", {"title": "Trip"})
        return _call(
            "c2",
            "submit_verdict",
            {
                "outcome": "success",
                "score": 1.0,
                "state_checks": [
                    {
                        "check": "note has flights",
                        "passed": True,
                        "evidence": "v1\nflights",
                    }
                ],
                "reply_accuracy": "accurate",
                "unwanted_effects": [],
                "rationale": "The note was updated as asked.",
            },
        )

    llm = RuleBasedMockLLMClient(rules=[(lambda _args: True, respond)])
    evidence = TrialEvidence(
        prompt="Add the flights to the trip note",
        asked_at="2026-09-09T13:30:00+10:00",
        reply="Done.",
        transcript=[],
        reference_reply="I added the flights.",
        reference_tools=["get_note", "add_or_update_note"],
    )

    verdict, calls = await grade_trial(
        llm,
        evidence,
        base_url="http://sandbox",
        token="t",
        transport=httpx.MockTransport(sandbox),
    )

    assert verdict is not None
    assert verdict.outcome == "success"
    assert executed == ["/api/tools/execute/get_note"]
    assert [c["tool"] for c in calls] == ["get_note"]
    assert all("add_or_update_note" not in names for names in offered)
    assert "submit_verdict" in offered[0]
