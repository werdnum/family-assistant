"""Replay one recorded assistant turn against a shipped model tier.

A case is the context a historical turn ran with -- system prompt, history up
to and including the user message that started the turn, the tool definitions
the model was offered -- plus recorded results for the tools it may call. The
runner drives the model's tool loop for that one turn, answering each call
from the recording, and reports what the model did before its final reply:
which tools it called, whether the calls a good answer needs were among them,
how many requests and tokens it took and how long.

It exists to compare a prompt or recipe change on the turn that motivated it,
with the shipped recipe as the control: ``with_custom_instructions=False``
builds the same chain entry with its ``custom_instructions`` removed. Cases
quote household conversations, so they live in the private eval tree, never in
the repository.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from family_assistant.llm.custom_instructions import CUSTOM_INSTRUCTIONS_KEY
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    dict_to_message,
)
from family_assistant.llm.model_tiers import resolve_entry_client_config
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.tools.on_demand import ACTIVATE_TOOLS_TOOL_NAME

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition

__all__ = [
    "RecordedToolResult",
    "ReplayCase",
    "ReplayExpectation",
    "ReplayTrial",
    "build_tier_entry_client",
    "run_trial",
]

UNRECORDED_RESULT = (
    "Error: this replay has no recorded result for that call. Answer from what "
    "you already have."
)


class RecordedToolResult(BaseModel):
    """A tool result the replay hands back when a call matches."""

    name: str
    arguments_contain: dict[str, str] = Field(default_factory=dict)
    """Substrings each named argument must contain, case-folded."""
    result: str


class ReplayExpectation(BaseModel):
    """What a good turn does, checked deterministically."""

    required_tools_any: list[str] = Field(default_factory=list)
    """At least one of these must be called before the final reply."""
    max_tool_calls: int | None = None
    """For a control case that is already answerable: the most calls tolerated."""
    forbidden_reply_patterns: list[str] = Field(default_factory=list)
    """Regexes the final reply must not match, e.g. "haven'?t checked"."""


class ReplayCase(BaseModel):
    id: str
    description: str = ""
    system_prompt: str
    # ast-grep-ignore: no-dict-any - messages in the stored message_to_dict shape
    messages: list[dict[str, Any]]
    # ast-grep-ignore: no-dict-any - OpenAI-style tool definitions as offered
    tools: list[dict[str, Any]] = Field(default_factory=list)
    # ast-grep-ignore: no-dict-any - OpenAI-style tool definitions as offered
    deferred_tools: list[dict[str, Any]] = Field(default_factory=list)
    """On-demand tools, offered once an ``activate_tools`` call names them.

    As in production, an activation appends to the tool list for the rest of
    the turn. The case's ``tools`` should include ``activate_tools`` itself and
    the system prompt the on-demand catalog, as the turn being replayed had.
    """
    recorded_tool_results: list[RecordedToolResult] = Field(default_factory=list)
    expect: ReplayExpectation = Field(default_factory=ReplayExpectation)


@dataclass
class ReplayTrial:
    case_id: str
    variant: str
    model: str
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    unrecorded_calls: int = 0
    requests: int = 0
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0
    completion_tokens: int = 0
    duration_s: float = 0.0
    final_reply: str | None = None
    hit_iteration_limit: bool = False
    passed: bool = False
    failures: list[str] = field(default_factory=list)


def build_tier_entry_client(
    config: AppConfig,
    tier_id: str,
    entry_index: int,
    *,
    with_custom_instructions: bool,
) -> LLMInterface:
    """The client one shipped chain entry builds, optionally without its policy."""
    entry = config.model_tiers[tier_id].chain[entry_index]
    if not with_custom_instructions and entry.llm_parameters:
        entry = entry.model_copy(
            update={
                "llm_parameters": {
                    k: v
                    for k, v in entry.llm_parameters.items()
                    if k != CUSTOM_INSTRUCTIONS_KEY
                }
            }
        )
    llm_parameters = config.llm_parameters
    if not with_custom_instructions:
        llm_parameters = {
            pattern: {k: v for k, v in params.items() if k != CUSTOM_INSTRUCTIONS_KEY}
            for pattern, params in llm_parameters.items()
        }
    return LLMClientFactory.create_client(
        resolve_entry_client_config(entry, llm_parameters)
    )


# ast-grep-ignore: no-dict-any - messages in the stored message_to_dict shape
def _history_message(data: dict[str, Any]) -> LLMMessage:
    calls = data.get("tool_calls")
    if data.get("role") != "assistant" or not calls:
        return dict_to_message(data)
    return AssistantMessage(
        content=data.get("content"),
        tool_calls=[
            ToolCallItem(
                id=call["id"],
                type="function",
                function=ToolCallFunction(
                    name=call["function"]["name"],
                    arguments=call["function"]["arguments"],
                ),
            )
            for call in calls
        ],
    )


def _arguments(call: ToolCallItem) -> dict[str, object]:
    raw = call.function.arguments
    if isinstance(raw, dict):
        return raw
    parsed = json.loads(raw) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _recorded_result(case: ReplayCase, call: ToolCallItem) -> str | None:
    arguments = _arguments(call)
    for recorded in case.recorded_tool_results:
        if recorded.name != call.function.name:
            continue
        if all(
            value.casefold() in str(arguments.get(key, "")).casefold()
            for key, value in recorded.arguments_contain.items()
        ):
            return recorded.result
    return None


def _activate(
    call: ToolCallItem,
    # ast-grep-ignore: no-dict-any - OpenAI-style tool definitions as offered
    deferred: dict[str, dict[str, Any]],
    tools: list[ToolDefinition],
) -> str:
    requested = _arguments(call).get("tool_names") or []
    names = [
        str(name)
        for name in (requested if isinstance(requested, list) else [])
        if str(name) in deferred
    ]
    for name in names:
        tools.append(deferred.pop(name))  # type: ignore[arg-type]  # case JSON is the ToolDefinition shape
    if not names:
        return "No matching on-demand tools to activate."
    return (
        f"Activated tools: {', '.join(names)}. You can now use them for the "
        "rest of the conversation."
    )


def _grade(case: ReplayCase, trial: ReplayTrial) -> None:
    called = {str(call["name"]) for call in trial.tool_calls}
    expect = case.expect
    if expect.required_tools_any and not called & set(expect.required_tools_any):
        trial.failures.append(
            f"none of {expect.required_tools_any} called before the reply"
        )
    if expect.max_tool_calls is not None and (
        len(trial.tool_calls) > expect.max_tool_calls
    ):
        trial.failures.append(
            f"{len(trial.tool_calls)} tool calls, more than {expect.max_tool_calls}"
        )
    reply = trial.final_reply or ""
    trial.failures.extend(
        f"reply matches {pattern!r}"
        for pattern in expect.forbidden_reply_patterns
        if re.search(pattern, reply, re.IGNORECASE)
    )
    if trial.hit_iteration_limit:
        trial.failures.append("hit the iteration limit without a final reply")
    trial.passed = not trial.failures


async def run_trial(
    client: LLMInterface,
    case: ReplayCase,
    *,
    variant: str,
    model: str,
    max_iterations: int = 12,
) -> ReplayTrial:
    """Drive one turn of ``case`` to its final reply and grade it."""
    trial = ReplayTrial(case_id=case.id, variant=variant, model=model)
    messages: list[LLMMessage] = [
        SystemMessage(content=case.system_prompt),
        *(_history_message(m) for m in case.messages),
    ]
    tools: list[ToolDefinition] = list(case.tools)  # type: ignore[arg-type]  # case JSON is the ToolDefinition shape
    deferred = {d["function"]["name"]: d for d in case.deferred_tools}
    started = time.monotonic()
    for _ in range(max_iterations):
        output = await client.generate_response(messages, tools or None)
        trial.requests += 1
        usage = output.reasoning_info or {}
        trial.prompt_tokens += usage.get("prompt_tokens", 0)
        trial.cached_prompt_tokens += usage.get("cached_prompt_tokens", 0)
        trial.cache_write_tokens += usage.get("cache_write_tokens", 0)
        trial.completion_tokens += usage.get("completion_tokens", 0)
        if output.resolved_model:
            trial.model = output.resolved_model
        if not output.tool_calls:
            trial.final_reply = output.content
            break
        messages.append(
            AssistantMessage(
                content=output.content,
                tool_calls=output.tool_calls,
                provider_metadata=output.provider_metadata,
            )
        )
        for call in output.tool_calls:
            result = _recorded_result(case, call)
            trial.tool_calls.append({
                "name": call.function.name,
                "arguments": _arguments(call),
                "recorded": result is not None,
            })
            if call.function.name == ACTIVATE_TOOLS_TOOL_NAME:
                result = _activate(call, deferred, tools)
            if result is None:
                trial.unrecorded_calls += 1
            messages.append(
                ToolMessage(
                    tool_call_id=call.id,
                    name=call.function.name,
                    content=result if result is not None else UNRECORDED_RESULT,
                )
            )
    else:
        trial.hit_iteration_limit = True
    trial.duration_s = time.monotonic() - started
    _grade(case, trial)
    return trial
