"""Meta-tools that let a Live (voice) session reach tools it never declared.

A Gemini Live session fixes its tool declarations in the ``setup`` message and
has no way to add one mid-session, so the LLM loop's ``activate_tools`` — which
works by handing the model a larger tool list on the next iteration — cannot be
used there. Instead a live session declares two tools whose schemas never
change:

* ``search_tools`` returns matching tools with their full argument schema, and
* ``call_tool`` runs one of them.

``LiveMetaToolsProvider`` is the chokepoint that implements both. It wraps the
profile's ordinary provider chain, so ``call_tool`` dispatches the inner call
back into that chain and inherits policy evaluation, taint tracking, tool-call
review and metrics unchanged. Any name that is not a meta-tool is delegated
untouched, which is what lets a single provider serve both the declaration list
and every execution a live session performs.

See docs/design/voice-mode-on-demand-tools.md.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from family_assistant.tools.infrastructure import (
    ToolDescriptorProvider,
    get_tool_definitions_for_advertisement,
)
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.tools.metadata import ToolDescriptor
    from family_assistant.tools.on_demand import OnDemandToolsView
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)

SEARCH_TOOLS_TOOL_NAME = "search_tools"
CALL_TOOL_TOOL_NAME = "call_tool"
LIVE_META_TOOL_NAMES = frozenset({SEARCH_TOOLS_TOOL_NAME, CALL_TOOL_TOOL_NAME})

DEFAULT_SEARCH_RESULT_LIMIT = 5
MAX_SEARCH_RESULT_LIMIT = 15

CATALOG_HEADING = "## Tools you can reach with search_tools"
CATALOG_INSTRUCTION = (
    "These tools exist but are not declared in this session. Call "
    "`search_tools` to get one's argument schema, then run it with "
    "`call_tool`. Do not guess a tool's arguments without searching first."
)

SEARCH_TOOLS_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SEARCH_TOOLS_TOOL_NAME,
        "description": (
            "Find tools that are not declared in this session. Returns each "
            "match's name, description and the full JSON Schema of its "
            "arguments, which is what you need to run it with call_tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords describing what you want to do, or the name "
                        "of a tool from the catalog. Omit to list everything."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"How many tools to return. Defaults to "
                        f"{DEFAULT_SEARCH_RESULT_LIMIT}, maximum "
                        f"{MAX_SEARCH_RESULT_LIMIT}."
                    ),
                },
            },
        },
    },
}

CALL_TOOL_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": CALL_TOOL_TOOL_NAME,
        "description": (
            "Run a tool that is not declared in this session. Look its "
            "argument schema up with search_tools first; a tool called with "
            "guessed arguments will fail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact name of the tool to run.",
                },
                "arguments_json": {
                    "type": "string",
                    "description": (
                        "The tool's arguments as a JSON object encoded in a "
                        "string, matching the schema search_tools returned, "
                        'e.g. {"title": "Groceries"}. Use {} for a tool that '
                        "takes no arguments."
                    ),
                },
            },
            "required": ["name"],
        },
    },
}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _score(name: str, description: str, query_tokens: list[str]) -> int:
    """Rank a tool against query tokens, weighting the name above the prose.

    A live session's catalog lists tool names, so the common search is for a
    name the model has already seen; matching the description is the fallback
    for a model that only knows what it wants to do.
    """
    name_tokens = set(_tokenize(name))
    description_tokens = set(_tokenize(description))
    score = 0
    for token in query_tokens:
        if token in name_tokens:
            score += 3
        elif any(token in candidate for candidate in name_tokens):
            score += 2
        elif token in description_tokens:
            score += 1
    return score


class LiveMetaToolsProvider:
    """Provider that adds ``search_tools``/``call_tool`` to a live session.

    Wraps an ``OnDemandToolsView``'s provider chain: the view decides which
    tools stay out of the declaration list, and this provider gives the model
    the only way to reach them. A live session can never confirm a tool call
    (neither live path installs a confirmation callback), so everything this
    provider advertises, searches and runs is restricted to what the policy
    layer permits with ``can_confirm=False``.
    """

    def __init__(
        self,
        on_demand_view: OnDemandToolsView,
        *,
        search_result_limit: int = DEFAULT_SEARCH_RESULT_LIMIT,
    ) -> None:
        self._view = on_demand_view
        self._wrapped_provider = on_demand_view.wrapped_provider
        self._search_result_limit = max(
            1, min(search_result_limit, MAX_SEARCH_RESULT_LIMIT)
        )

    @property
    def wrapped_provider(self) -> ToolsProvider:
        """Return the provider chain this one delegates to."""
        return self._wrapped_provider

    # --- Declarations -----------------------------------------------------

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return the eager tools plus the meta-tools, if any are configured.

        The test is what the profile *configures* as on-demand, not what is
        reachable right now. A live session declares once and cannot be given a
        declaration later, so an MCP server that happens to be down at session
        setup would otherwise cost the session its only route to that server's
        tools for good — the descriptor cache recovers when the health loop
        reconnects, but the declaration list never does. The LLM loop can
        afford the tighter test because it rebuilds its list every turn.
        """
        visible = await self._view.get_visible_definitions(can_confirm=False)
        if not self._view.has_on_demand_tools():
            return visible.definitions
        return [*visible.definitions, SEARCH_TOOLS_DEFINITION, CALL_TOOL_DEFINITION]

    async def get_system_prompt_addition(self) -> str | None:
        """Return the catalog of reachable-but-undeclared tools, or None."""
        catalog = await self._view.get_on_demand_catalog(can_confirm=False)
        if not catalog.entries:
            return None
        return catalog.render_for_system_prompt(
            heading=CATALOG_HEADING, instruction=CATALOG_INSTRUCTION
        )

    # --- Execution --------------------------------------------------------

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from the LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Handle the meta-tools; delegate every other name unchanged."""
        if name == SEARCH_TOOLS_TOOL_NAME:
            return await self._search_tools(arguments)
        if name == CALL_TOOL_TOOL_NAME:
            return await self._call_tool(arguments, context, call_id)
        return await self._wrapped_provider.execute_tool(
            name, arguments, context, call_id
        )

    async def _advertised_definitions(self) -> dict[str, ToolDefinition]:
        """Every tool this live session may run, keyed by name."""
        definitions = await get_tool_definitions_for_advertisement(
            self._wrapped_provider, can_confirm=False
        )
        return {
            name: definition
            for definition in definitions
            if (name := definition.get("function", {}).get("name")) is not None
        }

    # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from the LLM
    async def _search_tools(self, arguments: dict[str, Any]) -> ToolResult:
        raw_query = arguments.get("query")
        query = raw_query.strip() if isinstance(raw_query, str) else ""
        limit = self._resolve_limit(arguments.get("limit"))

        advertised = await self._advertised_definitions()
        visible = await self._view.get_visible_definitions(can_confirm=False)
        query_tokens = _tokenize(query)

        scored: list[tuple[int, str, ToolDefinition]] = []
        for name, definition in advertised.items():
            function = definition.get("function", {})
            description = function.get("description", "")
            score = _score(name, description, query_tokens) if query_tokens else 1
            if score:
                scored.append((score, name, definition))
        scored.sort(key=lambda item: (-item[0], item[1]))

        results = [
            {
                "name": name,
                "description": definition.get("function", {}).get("description", ""),
                "parameters": definition.get("function", {}).get(
                    "parameters", {"type": "object", "properties": {}}
                ),
                "already_declared": name not in visible.hidden_names,
            }
            for _, name, definition in scored[:limit]
        ]
        # ast-grep-ignore: no-dict-any - JSON tool result handed straight to the model
        payload: dict[str, Any] = {
            "tools": results,
            "match_count": len(scored),
            "returned": len(results),
        }
        if not results:
            payload["hint"] = (
                "No tool matched. Search again with a single keyword, or with "
                "no query at all to list everything available."
            )
        logger.info(
            "search_tools(query=%r) matched %d tools, returned %d",
            query,
            len(scored),
            len(results),
        )
        return ToolResult(data=payload)

    def _resolve_limit(self, raw_limit: object) -> int:
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            return self._search_result_limit
        return max(1, min(raw_limit, MAX_SEARCH_RESULT_LIMIT))

    async def _call_tool(
        self,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from the LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None,
    ) -> str | ToolResult:
        raw_name = arguments.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return ToolResult(
                text="call_tool needs a 'name'. Use search_tools to find one."
            )
        inner_name = raw_name.strip()
        if inner_name in LIVE_META_TOOL_NAMES:
            return ToolResult(
                text=(
                    f"call_tool cannot run '{inner_name}'. Pass the name of a "
                    "tool that search_tools returned."
                )
            )

        parsed = _parse_arguments_json(arguments.get("arguments_json"))
        if isinstance(parsed, str):
            return ToolResult(text=parsed)

        advertised = await self._advertised_definitions()
        if inner_name not in advertised:
            return ToolResult(
                text=(
                    f"Tool '{inner_name}' is not available in this voice "
                    "session. Use search_tools to find one that is."
                )
            )

        logger.info("call_tool dispatching to '%s'", inner_name)
        return await self._wrapped_provider.execute_tool(
            inner_name, parsed, context, call_id
        )

    # --- Delegation -------------------------------------------------------

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        """Delegate descriptors; the meta-tools have none of their own."""
        return await self._descriptor_provider().get_tool_descriptors()

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        """Delegate a single descriptor lookup."""
        return await self._descriptor_provider().get_tool_descriptor(name)

    def _descriptor_provider(self) -> ToolDescriptorProvider:
        provider = self._wrapped_provider
        if not isinstance(provider, ToolDescriptorProvider):
            msg = "Wrapped provider does not expose tool descriptors."
            raise TypeError(msg)
        return provider

    async def close(self) -> None:
        """Close the wrapped provider chain."""
        await self._wrapped_provider.close()


def _parse_arguments_json(
    raw: object,
    # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from the LLM
) -> dict[str, Any] | str:
    """Return the parsed arguments object, or an error message for the model."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        # Some models emit a real object despite the declared string type.
        return raw
    if not isinstance(raw, str):
        return "'arguments_json' must be a JSON object encoded as a string."
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"'arguments_json' is not valid JSON: {exc}. Send a JSON object."
    if not isinstance(parsed, dict):
        return "'arguments_json' must decode to a JSON object, e.g. {\"key\": 1}."
    return parsed
