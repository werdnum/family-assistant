"""Tools the MCP adapter exposes.

``ask_family_assistant`` runs a question through a processing profile as the
authenticated user over the same non-streaming turn the REST chat endpoint uses,
so conversation ownership, one-turn-per-conversation and durable deferred
confirmations are inherited rather than re-implemented. A turn that outlasts
``mcp_adapter.reply_wait_seconds`` keeps running, and
``get_family_assistant_reply`` collects it (see ``turns``).
"""

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Literal

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
from family_assistant.web.mcp_adapter.turns import RunningTurns, wait_for_turn
from family_assistant.web.models import ChatMessageResponse, ChatPromptRequest
from family_assistant.web.routers.chat_api import latest_reply, run_non_streaming_turn
from family_assistant.web.web_chat_interface import WebChatInterface

logger = logging.getLogger(__name__)

MCP_INTERFACE_TYPE = "mcp"


STILL_WORKING_MESSAGE = (
    "Family Assistant is still working on this. Call get_family_assistant_reply "
    "with this conversation_id to wait for the reply."
)


class AskFamilyAssistantResult(BaseModel):
    """What ``ask_family_assistant`` and ``get_family_assistant_reply`` return."""

    status: Literal["complete", "working"] = Field(
        description=(
            "complete: reply holds the answer. working: the answer is not ready "
            "yet; call get_family_assistant_reply with conversation_id to wait for it."
        )
    )
    reply: str | None = Field(
        description="The assistant's answer, once status is complete."
    )
    conversation_id: str = Field(
        description=(
            "The conversation this exchange belongs to. Pass it back as "
            "conversation_id to continue the same conversation."
        )
    )
    message: str | None = Field(
        default=None, description="What to do next while status is working."
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


def _mcp_chat_interface(request: Request) -> WebChatInterface:
    """The interface that saves into MCP conversations, registered at startup.

    Deferred work started from an MCP turn (an approved confirmation's result)
    is delivered through ``chat_interfaces[MCP_INTERFACE_TYPE]``; the turn uses
    the same one so everything it leaves behind lands where the client's next
    call will read it.
    """
    interfaces = getattr(request.app.state, "chat_interfaces", None) or {}
    interface = interfaces.get(MCP_INTERFACE_TYPE)
    if not isinstance(interface, WebChatInterface):
        raise ToolError("MCP adapter has no registered chat interface.")
    return interface


def _tool_error_for(exc: HTTPException) -> ToolError:
    """Translate a failure of the shared turn into a tool result the caller reads."""
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return ToolError("Conversation not found: it does not exist or is not yours.")
    if exc.status_code == status.HTTP_409_CONFLICT:
        return ToolError(
            "Another turn is in progress in this conversation; retry shortly."
        )
    return ToolError(str(exc.detail))


def _working(conversation_id: str) -> AskFamilyAssistantResult:
    return AskFamilyAssistantResult(
        status="working",
        reply=None,
        conversation_id=conversation_id,
        message=STILL_WORKING_MESSAGE,
    )


def _finished_turn_result(
    task: asyncio.Task[ChatMessageResponse],
) -> AskFamilyAssistantResult:
    """The result of a turn that has ended: its reply, or its failure as a tool error."""
    if task.cancelled():
        raise ToolError(
            "This turn was stopped before it finished. Ask again with "
            "ask_family_assistant and the same conversation_id."
        )
    try:
        response = task.result()
    except HTTPException as exc:
        raise _tool_error_for(exc) from exc
    return AskFamilyAssistantResult(
        status="complete",
        reply=response.reply,
        conversation_id=response.conversation_id,
    )


def register_tools(mcp: FastMCP, running_turns: RunningTurns) -> None:
    """Register the adapter's tools on ``mcp``, running turns in ``running_turns``."""

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

        Some requests take several minutes. If the result's status is "working",
        the answer is not ready yet: call get_family_assistant_reply with its
        conversation_id, and keep calling it while the status stays "working".
        """
        request = ctx.request_context.request
        if request is None:
            raise ToolError("ask_family_assistant needs an HTTP request context.")
        current_user = await get_current_user(request)
        processing_service = _select_processing_service(request)
        conversation_id = conversation_id or f"mcp-{uuid.uuid4()}"
        if running_turns.is_running(conversation_id):
            raise ToolError(
                "A request is still running in this conversation. Call "
                "get_family_assistant_reply with this conversation_id for its reply, "
                "then ask again."
            )
        payload = ChatPromptRequest(
            prompt=question,
            conversation_id=conversation_id,
            interface_type=MCP_INTERFACE_TYPE,
        )
        user_id = str(current_user["user_identifier"])
        task = running_turns.start(
            conversation_id,
            user_id,
            run_non_streaming_turn(
                request,
                current_user,
                Database(request.app.state.database_engine),
                payload,
                processing_service=processing_service,
                web_chat_interface=_mcp_chat_interface(request),
                initial_taint_sources=(mcp_caller_taint_source(current_user),),
            ),
        )
        if not await wait_for_turn(
            task, request.app.state.config.mcp_adapter.reply_wait_seconds
        ):
            return _working(conversation_id)
        return _finished_turn_result(task)

    @mcp.tool()
    async def get_family_assistant_reply(
        conversation_id: str,
        ctx: Context,
    ) -> AskFamilyAssistantResult:
        """Wait for the reply to an ask_family_assistant call that is still working.

        Pass the conversation_id from a result whose status was "working".
        Returns the reply once it is ready; if the status is still "working",
        call this again with the same conversation_id.
        """
        request = ctx.request_context.request
        if request is None:
            raise ToolError("get_family_assistant_reply needs an HTTP request context.")
        current_user = await get_current_user(request)
        task = running_turns.get(conversation_id, str(current_user["user_identifier"]))
        if task is not None:
            if not await wait_for_turn(
                task, request.app.state.config.mcp_adapter.reply_wait_seconds
            ):
                return _working(conversation_id)
            return _finished_turn_result(task)
        try:
            reply = await latest_reply(
                request,
                current_user,
                Database(request.app.state.database_engine),
                conversation_id,
                interface_type=MCP_INTERFACE_TYPE,
            )
        except HTTPException as exc:
            raise _tool_error_for(exc) from exc
        if reply is None:
            raise ToolError(
                "The latest request in this conversation ended without a reply; it "
                "may have been interrupted by a restart. Ask again with "
                "ask_family_assistant and the same conversation_id."
            )
        return AskFamilyAssistantResult(
            status="complete", reply=reply, conversation_id=conversation_id
        )
