"""Grade a workhorse trial by inspecting the household it left behind.

A trial is judged on outcomes, not on the path to them: did the calendar,
the notes, the house, the task list and the mail end up as the request
needed, and was the reply true. Rather than reading a log of actions, the
grader is itself an agent holding the household's own read tools -- the
same tools the assistant has, called through the sandbox's tool API as the
household user -- so it looks at the state the way the household would.
Writes are not offered to it.

It is given the request, when it was asked, what the candidate did and said,
and what the household's assistant did at the time as a reference (usually
right, not ground truth), and it ends by calling ``submit_verdict``.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

from family_assistant.llm.messages import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.tools.types import ToolDefinition, ToolFunctionSchema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.json_schema import JsonSchemaObject
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage

__all__ = [
    "READ_TOOL_PATTERN",
    "StateCheck",
    "TrialEvidence",
    "Verdict",
    "grade_trial",
    "read_tool_definitions",
]

# The household's read tools. Matched by name because tool definitions carry
# no tags over the API; a write tool slipping through would still be refused
# by nothing, so the pattern is deliberately conservative.
READ_TOOL_PATTERN = re.compile(
    r"^(get_|list_|search_|read_|find_|query_|render_home_assistant_template$"
    r"|download_state_history$|jq_query$"
    r"|gmail_search$|gmail_get_message$|drive_search$|drive_get_file$"
    r"|tuit_(now|find_tasks|get_task|get_changes|explain|list_queues|preview_queue)$"
    r"|ha_(search|call_read_tool|search_tools|get_[a-z_]+|config_get_[a-z_]+)$)"
)
_MAX_STEPS = 30
_RESULT_LIMIT = 12_000
_VERDICT_TOOL = "submit_verdict"


class StateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str = Field(
        description="What was checked, e.g. 'dentist event moved to 9 Oct 12:30'."
    )
    passed: bool
    evidence: str = Field(description="What the read tools showed.")


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["success", "partial", "failure", "unscorable"] = Field(
        description=(
            "success: the household ended up as the request needed and the reply "
            "was true; partial: some of it; failure: not done, done wrongly, or a "
            "false reply; unscorable: the sandbox could not support the task "
            "(a missing tool or data), so it says nothing about the model."
        )
    )
    score: float = Field(ge=0.0, le=1.0)
    state_checks: list[StateCheck]
    reply_accuracy: Literal["accurate", "minor_issues", "inaccurate", "no_reply"]
    unwanted_effects: list[str] = Field(
        description="Changes the request did not ask for: wrong item, duplicates, extra messages."
    )
    rationale: str


class TrialEvidence(BaseModel):
    """What the grader is told about the trial."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    asked_at: str
    reply: str | None
    transcript: list[dict[str, object]]
    reference_reply: str | None
    reference_tools: list[str]
    recorded_writes: list[dict[str, object]] = []


_INSTRUCTIONS = """You grade one turn of a household assistant, run in a sandbox copy of \
the household at the moment the request was originally made. Judge the OUTCOME: inspect the \
household's state with your read tools (calendar, notes, documents, mail, drive, smart home, \
task tracker, reminders and callbacks) and decide whether it ended up as the request needed, \
and whether the assistant's reply to the user was true and useful.

- Check state yourself; do not trust the assistant's transcript or its claims. The transcript \
says where to look.
- The reference is what the household's assistant did at the time. It is usually right, but not \
ground truth: a different correct approach is a success.
- Anything changed that the request did not ask for (wrong item, duplicates, deleting more than \
asked) is an unwanted effect and lowers the outcome.
- A request that needed information or a tool the sandbox could not provide is unscorable, not \
a failure; say which.
- Asking a sensible clarifying question when the request is genuinely ambiguous can be a success.
- Times are in the household's timezone; the request was made at the given time.

Use as few tool calls as you need, then call submit_verdict exactly once."""


async def read_tool_definitions(
    client: httpx.AsyncClient,
) -> list[ToolDefinition]:
    """The sandbox's read tools, as the grader is offered them."""
    response = await client.get("/api/tools/definitions")
    response.raise_for_status()
    return [
        definition
        for definition in response.json()["tools"]
        if READ_TOOL_PATTERN.match(str(definition.get("function", {}).get("name", "")))
    ]


def _verdict_tool() -> ToolDefinition:
    return ToolDefinition(
        type="function",
        function=ToolFunctionSchema(
            name=_VERDICT_TOOL,
            description="Record the verdict. Call exactly once, at the end.",
            # A schema from pydantic, cast at the boundary as json_schema says.
            parameters=cast("JsonSchemaObject", Verdict.model_json_schema()),
        ),
    )


async def _run_tool(
    client: httpx.AsyncClient,
    name: str,
    arguments: Mapping[str, object],
    profile_id: str,
) -> str:
    response = await client.post(
        f"/api/tools/execute/{name}",
        json={"arguments": dict(arguments), "profile_id": profile_id},
    )
    return response.text[:_RESULT_LIMIT]


def _evidence_message(evidence: TrialEvidence) -> str:
    return json.dumps(evidence.model_dump(), indent=1, default=str)


async def grade_trial(
    llm: LLMInterface,
    evidence: TrialEvidence,
    *,
    base_url: str,
    token: str,
    profile_id: str = "default_assistant",
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[Verdict | None, list[dict[str, object]]]:
    """Run the grading agent; return its verdict and the calls it made."""
    calls: list[dict[str, object]] = []
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=120,
        headers={"Authorization": f"Bearer {token}"},
        transport=transport,
    ) as client:
        tools = [*await read_tool_definitions(client), _verdict_tool()]
        allowed = {str(t["function"]["name"]) for t in tools}
        messages: list[LLMMessage] = [
            SystemMessage(content=_INSTRUCTIONS),
            UserMessage(content=f"The trial:\n{_evidence_message(evidence)}"),
        ]
        for _ in range(_MAX_STEPS):
            output = await llm.generate_response(messages, tools=tools)
            messages.append(
                AssistantMessage(content=output.content, tool_calls=output.tool_calls)
            )
            if not output.tool_calls:
                messages.append(
                    UserMessage(content=f"Finish by calling {_VERDICT_TOOL}.")
                )
                continue
            for call in output.tool_calls:
                name = call.function.name
                raw = call.function.arguments
                arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                if name == _VERDICT_TOOL:
                    return Verdict.model_validate(arguments), calls
                result = (
                    await _run_tool(client, name, arguments, profile_id)
                    if name in allowed
                    else f"Error: {name} is not available to the grader."
                )
                calls.append({
                    "tool": name,
                    "arguments": arguments,
                    "result": result[:500],
                })
                messages.append(
                    ToolMessage(
                        tool_call_id=call.id or str(uuid.uuid4()),
                        name=name,
                        content=result,
                    )
                )
    return None, calls


def grader_transcript(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """A trial transcript trimmed to what a grader needs to know where to look."""
    trimmed = []
    for row in rows:
        entry: dict[str, object] = {"role": row.get("role")}
        if row.get("tool_calls"):
            entry["tool_calls"] = row["tool_calls"]
        if row.get("content"):
            entry["content"] = str(row["content"])[:1500]
        if row.get("tool_name"):
            entry["tool_name"] = row["tool_name"]
        trimmed.append(entry)
    return trimmed
