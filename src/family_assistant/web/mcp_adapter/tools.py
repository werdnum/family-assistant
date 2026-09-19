"""Tools the MCP adapter exposes.

One tool, ``ask_family_assistant``, runs a question through a processing profile
as the authenticated user over the same non-streaming turn the REST chat
endpoint uses, so conversation ownership, one-turn-per-conversation and durable
deferred confirmations are inherited rather than re-implemented.
"""

import logging
import uuid
from collections.abc import Mapping

from fastapi import HTTPException, status
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from starlette.requests import Request

from family_assistant.processing import ProcessingService
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
)
from family_assistant.storage.database import Database
from family_assistant.web.dependencies import get_current_user
from family_assistant.web.models import ChatPromptRequest
from family_assistant.web.routers.chat_api import run_non_streaming_turn

logger = logging.getLogger(__name__)

MCP_INTERFACE_TYPE = "mcp"


class AskFamilyAssistantResult(BaseModel):
    """What ``ask_family_assistant`` returns."""

    reply: str = Field(description="The assistant's answer.")
    conversation_id: str = Field(
        description=(
            "The conversation this exchange belongs to. Pass it back as "
            "conversation_id to continue the same conversation."
        )
    )


def mcp_caller_taint_source(current_user: Mapping[str, object]) -> TaintSource:
    """The trust a question arriving over MCP carries.

    The text comes from a machine acting for the authenticated user rather than
    from the user directly, so it enters the turn at the tier the A2A endpoints
    give a peer's message: a recognized machine, not direct user input.
    """
    token_name = current_user.get("token_name")
    return TaintSource(
        source_type=TaintSourceType.MANUAL,
        source_id=f"mcp:{token_name}" if token_name else "mcp",
        tier=SourceTrustTier.RECOGNIZED_MACHINE,
        labels=frozenset({"source_recognized_machine"}),
        reason="Question relayed by an MCP client acting for the user.",
    )


def _select_processing_service(request: Request) -> ProcessingService:
    """The service the tool runs under, per ``mcp_adapter.profile_id``.

    A misconfigured profile id is an error the caller sees, not a silent fall
    back to the default profile.
    """
    profile_id = request.app.state.config.mcp_adapter.profile_id
    if profile_id is None:
        return request.app.state.processing_service
    registry = getattr(request.app.state, "processing_services", {})
    service = registry.get(profile_id)
    if service is None:
        raise ToolError(
            f"MCP adapter is configured with profile '{profile_id}', which does not exist."
        )
    if service.kind == "remote":
        raise ToolError(
            f"MCP adapter is configured with profile '{profile_id}', which is a "
            "remote delegation-only profile and cannot answer directly."
        )
    return service


def _tool_error_for(exc: HTTPException) -> ToolError:
    """Translate a failure of the shared turn into a tool result the caller reads."""
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return ToolError("Conversation not found: it does not exist or is not yours.")
    if exc.status_code == status.HTTP_409_CONFLICT:
        return ToolError(
            "Another turn is in progress in this conversation; retry shortly."
        )
    return ToolError(str(exc.detail))


def register_tools(mcp: FastMCP) -> None:
    """Register the adapter's tools on ``mcp``."""

    @mcp.tool()
    async def ask_family_assistant(
        question: str,
        ctx: Context,
        conversation_id: str | None = None,
    ) -> AskFamilyAssistantResult:
        """Ask the household's Family Assistant a question or give it an instruction.

        Family Assistant knows the family's notes, calendar, tasks, documents and
        smart home, and can act on them: answer questions about them, add or
        change entries, and schedule things. Ask in natural language, as you
        would ask a person, with enough context for it to act.

        The result carries a conversation_id. Pass it back as conversation_id on
        the next call to continue the same conversation (follow-ups, corrections,
        "yes, do that"); omit it to start a fresh one. An action that needs the
        user's approval is recorded for them to approve elsewhere and the reply
        says so.
        """
        request = ctx.request_context.request
        if request is None:
            raise ToolError("ask_family_assistant needs an HTTP request context.")
        current_user = await get_current_user(request)
        processing_service = _select_processing_service(request)
        payload = ChatPromptRequest(
            prompt=question,
            conversation_id=conversation_id or f"mcp-{uuid.uuid4()}",
            interface_type=MCP_INTERFACE_TYPE,
        )
        try:
            response = await run_non_streaming_turn(
                request,
                current_user,
                Database(request.app.state.database_engine),
                payload,
                processing_service=processing_service,
                web_chat_interface=request.app.state.web_chat_interface,
                initial_taint_sources=(mcp_caller_taint_source(current_user),),
            )
        except HTTPException as exc:
            raise _tool_error_for(exc) from exc
        return AskFamilyAssistantResult(
            reply=response.reply, conversation_id=response.conversation_id
        )
