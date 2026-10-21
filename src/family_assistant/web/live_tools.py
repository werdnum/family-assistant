"""Tool wiring shared by the Live (voice) entry points.

Both live paths — the browser/iOS ephemeral-token flow and the Asterisk
telephony websocket — declare their tools once, at session setup, and can never
add one afterwards. They therefore resolve their provider and their tool
catalog the same way, through this module, so the two paths cannot drift apart
on what a voice session may reach.

See docs/design/voice-mode-on-demand-tools.md.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from family_assistant.config_models import GeminiLiveConfig
from family_assistant.tools.infrastructure import (
    get_tool_definitions_for_advertisement,
)
from family_assistant.tools.live_meta import CALL_TOOL_TOOL_NAME, LiveMetaToolsProvider
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService
    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

ATTACHMENT_READER_TOOL_NAMES = frozenset({"jq_query", "read_text_attachment"})


async def resolve_live_tools(
    service: ProcessingService,
    *,
    on_demand: bool,
) -> tuple[ToolsProvider, str | None]:
    """Return the provider a live session declares from, and its catalog text.

    With ``on_demand`` set the session declares only the eager tools plus
    ``search_tools``/``call_tool``, and the returned text lists the tools those
    meta-tools can reach so the model knows they exist. With it unset — or for
    a profile that configures nothing as on-demand — the session declares every
    advertisable tool and there is no catalog to add.
    """
    provider = service.live_tools_provider
    if not on_demand or not isinstance(provider, LiveMetaToolsProvider):
        return service.tools_provider, None
    return provider, await provider.get_system_prompt_addition()


async def execute_live_tool(
    service: ProcessingService,
    name: str,
    # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from the model
    arguments: dict[str, Any],
    context: ToolExecutionContext,
    call_id: str | None = None,
) -> str | ToolResult:
    """Run a tool a Live session called, keeping a large result out of its context.

    A Live model prefills the whole function response before it speaks, so a
    result the chat loop would take in stride is heard as dead air. One past
    ``gemini_live_config.large_tool_result_threshold_kb`` is stored as an
    attachment, and the model gets the same pointer and ``jq_query`` /
    ``read_text_attachment`` hint the chat loop gives it. A profile that can
    reach neither reader gets the whole result: slow, but a pointer it cannot
    follow would leave it with nothing.
    """
    result = await service.live_tools_provider.execute_tool(
        name, arguments, context, call_id
    )
    live_config = (
        service.app_config.gemini_live_config
        if service.app_config is not None
        else GeminiLiveConfig()
    )
    content = _live_response_text(result)
    threshold_kb = live_config.large_tool_result_threshold_kb
    if len(content.encode("utf-8")) < threshold_kb * 1024:
        return result
    if not await _can_read_attachments(service):
        logger.info(
            "Returning a large '%s' result whole: profile '%s' has no attachment reader",
            name,
            service.service_config.id,
        )
        return result
    taint_tracker = context.taint_tracker
    stub, attachment_id = await service.attachment_processor.handle_large_result(
        context.db_context,
        content,
        _target_tool_name(name, arguments),
        context.conversation_id,
        call_id or context.turn_id or "",
        taint_tracker.snapshot().to_metadata() if taint_tracker else None,
        # The session belongs to one caller, so the result is theirs alone.
        owner_user_id=context.user_id,
        threshold_kb=threshold_kb,
    )
    if attachment_id is None:
        return result
    attachments = result.attachments if isinstance(result, ToolResult) else None
    return ToolResult(text=stub, attachments=attachments)


async def _can_read_attachments(service: ProcessingService) -> bool:
    """Whether the session can follow a large-result pointer.

    Everything advertised is reachable from a Live session, directly or through
    ``call_tool``, so this is the same list the session is declared from.
    """
    definitions = await get_tool_definitions_for_advertisement(
        service.tools_provider, can_confirm=False
    )
    return any(
        definition.get("function", {}).get("name") in ATTACHMENT_READER_TOOL_NAMES
        for definition in definitions
    )


# ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from the model
def _target_tool_name(name: str, arguments: dict[str, Any]) -> str:
    """The tool that produced the result, looking through ``call_tool``.

    The large-result exemptions and the pointer text are about that tool, so a
    wrapped ``read_text_attachment`` must not be converted where a declared one
    would not.
    """
    inner = arguments.get("name") if name == CALL_TOOL_TOOL_NAME else None
    return inner.strip() if isinstance(inner, str) and inner.strip() else name


def _live_response_text(result: object) -> str:
    """Everything a Live entry point would put in the function response."""
    if isinstance(result, str):
        return result
    if isinstance(result, ToolResult):
        if result.data is None:
            return result.text or ""
        if result.text is None:
            return result.get_text()
        return json.dumps({"text": result.text, "data": result.data}, default=str)
    return json.dumps(result, default=str)
