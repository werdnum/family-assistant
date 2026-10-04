"""Deferred tools: declared on every request, usable once activated.

The LLM loop passes every tool a profile may use on every request, marking the
on-demand ones ``defer_loading``. Activation is recorded on the tool message
that performed it (``ToolMessage.activated_tools``) and persisted with the
conversation, so whether a deferred tool is usable is a property of the
messages a request carries, not of turn-local state.

Each provider adapter renders that: Anthropic natively, with ``defer_loading``
and appended ``tool_addition`` blocks, so its ``tools`` array never changes;
every other adapter by narrowing the list with ``resolve_deferred_tools``.
Keeping the decision in the adapter is what lets the same request fall back to
a different provider and still be rendered correctly. See
docs/design/append-only-prompt.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.llm.messages import ToolMessage

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition


def tool_name(tool: ToolDefinition) -> str | None:
    """The function name a tool definition declares."""
    function = tool.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) else None


def is_deferred(tool: ToolDefinition) -> bool:
    """Whether *tool* is declared but unusable until activated."""
    return bool(tool.get("defer_loading"))


def activated_tool_names(messages: Iterable[LLMMessage]) -> frozenset[str]:
    """Every tool activated by a tool message in *messages*."""
    return frozenset(
        name
        for message in messages
        if isinstance(message, ToolMessage) and message.activated_tools
        for name in message.activated_tools
    )


def strip_deferral(tool: ToolDefinition) -> ToolDefinition:
    """*tool* without its ``defer_loading`` marker."""
    if "defer_loading" not in tool:
        return tool
    return {"type": tool["type"], "function": tool["function"]}


def resolve_deferred_tools(
    tools: list[ToolDefinition] | None,
    messages: Sequence[LLMMessage],
) -> list[ToolDefinition] | None:
    """The tools usable for a request, for a provider with no deferral of its own.

    Non-deferred tools, plus the deferred ones some message in *messages*
    activated, with the marker removed. ``None`` stays ``None``.
    """
    if tools is None:
        return None
    activated = activated_tool_names(messages)
    return [
        strip_deferral(tool)
        for tool in tools
        if not is_deferred(tool) or tool_name(tool) in activated
    ]
