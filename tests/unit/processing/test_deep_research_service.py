"""Tests for DeepResearchProcessingService's submit/poll/cancel primitives."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.processing.deep_research_service import (
    DeepResearchProcessingService,
)
from family_assistant.processing.protocol import PENDING
from family_assistant.processing.types import (
    ChatInteractionResult,
    ProcessingServiceConfig,
)
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.storage.repositories.delegation_runs import (
        DelegationRunCreate,
    )
    from family_assistant.tools.types import ToolExecutionContext, ToolResult


class SimpleToolsProvider:
    async def get_tool_definitions(self) -> list:
        return []

    async def execute_tool(
        self,
        name: str,
        arguments: dict,
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return ""

    async def close(self) -> None:
        pass


def _make_service(llm_client: GoogleGenAIClient) -> DeepResearchProcessingService:
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a research assistant for {user_name}."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="research",
    )
    return DeepResearchProcessingService(
        llm_client=llm_client,
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )


def _google_client() -> GoogleGenAIClient:
    return GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")


@pytest.mark.asyncio
async def test_submit_async_renders_prompt_and_starts_interaction(
    db_engine: AsyncEngine,
) -> None:
    """submit_async renders the system prompt and forwards content as input."""
    llm_client = _google_client()
    mock_interaction = AsyncMock()
    mock_interaction.id = "inter_new"
    llm_client.start_deep_research_interaction = AsyncMock(
        return_value=mock_interaction
    )
    service = _make_service(llm_client)

    db_context = Database(db_engine)
    submission = await service.submit_async(
        [{"type": "text", "text": "Research quantum computing."}],
        conversation_id="conv-1",
        subconversation_id="sub-1",
        user_name="Andrew",
        db_context=db_context,
    )

    assert submission.remote_task_id == "inter_new"
    assert submission.remote_context_id is None
    assert submission.terminal_result is None

    call_args = llm_client.start_deep_research_interaction.call_args
    messages = call_args.args[0]
    assert any(
        m.role == "system" and "for Andrew" in (m.content or "") for m in messages
    )
    assert any(
        m.role == "user" and "Research quantum computing." in (m.content or "")
        for m in messages
    )
    assert call_args.kwargs["previous_interaction_id"] is None


@pytest.mark.asyncio
async def test_submit_async_chains_onto_prior_completed_run(
    db_engine: AsyncEngine,
) -> None:
    """A resumed delegation (same subconversation) chains previous_interaction_id."""
    llm_client = _google_client()
    mock_interaction = AsyncMock()
    mock_interaction.id = "inter_followup"
    llm_client.start_deep_research_interaction = AsyncMock(
        return_value=mock_interaction
    )
    service = _make_service(llm_client)

    db_context = Database(db_engine)
    run: DelegationRunCreate = {
        "delegation_id": "deleg-1",
        "task_id": "task-1",
        "source_profile_id": "default_assistant",
        "target_service_id": "research",
        "interface_type": "test",
        "conversation_id": "conv-2",
        "subconversation_id": "sub-2",
        "request_text": "Initial question",
        "content_parts_json": [{"type": "text", "text": "Initial question"}],
    }
    await db_context.delegation_runs.create_run(run)
    await db_context.delegation_runs.update_remote_task(
        "deleg-1", remote_task_id="inter_original", remote_context_id=None
    )
    await db_context.delegation_runs.mark_completed(
        delegation_id="deleg-1",
        result_text="The original answer.",
        result_attachment_ids=[],
        completed_at=datetime.now(UTC),
    )

    await service.submit_async(
        [{"type": "text", "text": "Tell me more."}],
        conversation_id="conv-2",
        subconversation_id="sub-2",
        user_name="Andrew",
        db_context=db_context,
    )

    call_args = llm_client.start_deep_research_interaction.call_args
    assert call_args.kwargs["previous_interaction_id"] == "inter_original"


@pytest.mark.asyncio
async def test_poll_async_pending_states() -> None:
    """in_progress/requires_action map to the PENDING sentinel."""
    llm_client = _google_client()
    service = _make_service(llm_client)

    for status in ("in_progress", "requires_action"):
        interaction = AsyncMock()
        interaction.status = status
        llm_client.get_agent_interaction = AsyncMock(return_value=interaction)
        result = await service.poll_async("inter_x", None)
        assert result is PENDING


@pytest.mark.asyncio
async def test_poll_async_unrecognized_status_stays_pending() -> None:
    """A status this SDK doesn't enumerate (e.g. capacity-queueing) must not fail the run.

    poll_async deny-lists known terminal-error statuses rather than
    allow-listing "pending" ones, so an unrecognized status like "queued"
    is treated as still pending instead of failing the delegation outright.
    """
    llm_client = _google_client()
    interaction = AsyncMock()
    interaction.status = "queued"
    llm_client.get_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(llm_client)

    result = await service.poll_async("inter_x", None)

    assert result is PENDING


@pytest.mark.asyncio
async def test_poll_async_completed_returns_success() -> None:
    """A completed interaction becomes a successful ChatInteractionResult."""
    llm_client = _google_client()
    interaction = AsyncMock()
    interaction.status = "completed"
    interaction.output_text = "The final research report."
    llm_client.get_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(llm_client)

    result = await service.poll_async("inter_x", None)

    assert isinstance(result, ChatInteractionResult)
    assert not result.has_error
    assert result.text_reply == "The final research report."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["failed", "cancelled", "incomplete", "budget_exceeded"]
)
async def test_poll_async_terminal_error_states_return_error_result(
    status: str,
) -> None:
    """Terminal non-success statuses become an error ChatInteractionResult."""
    llm_client = _google_client()
    interaction = AsyncMock()
    interaction.status = status
    llm_client.get_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(llm_client)

    result = await service.poll_async("inter_x", None)

    assert isinstance(result, ChatInteractionResult)
    assert result.has_error
    assert status in result.text_reply


@pytest.mark.asyncio
async def test_cancel_async_calls_through() -> None:
    """cancel_async delegates to the client's cancel primitive."""
    llm_client = _google_client()
    llm_client.cancel_agent_interaction = AsyncMock(return_value=None)
    service = _make_service(llm_client)

    await service.cancel_async("inter_x")

    llm_client.cancel_agent_interaction.assert_called_once_with("inter_x")


@pytest.mark.asyncio
async def test_cancel_async_swallows_errors() -> None:
    """A failed cancel is logged, not raised (must never abort the caller)."""
    llm_client = _google_client()
    llm_client.cancel_agent_interaction = AsyncMock(side_effect=RuntimeError("boom"))
    service = _make_service(llm_client)

    await service.cancel_async("inter_x")  # must not raise


def test_remote_context_id_is_always_none() -> None:
    """Deep Research has no context-grouping concept; always returns None."""
    service = _make_service(_google_client())
    assert service.remote_context_id("conv-1", "sub-1") is None
    assert service.remote_context_id("conv-1", None) is None


@pytest.mark.asyncio
async def test_google_client_type_guard_rejects_non_google_llm_client() -> None:
    """A misconfigured non-Google llm_client fails fast with a clear error.

    Exercised via poll_async (not cancel_async, which deliberately swallows
    every exception as best-effort).
    """
    fake_client = cast("Any", AsyncMock())
    service = _make_service(fake_client)

    with pytest.raises(TypeError, match="GoogleGenAIClient"):
        await service.poll_async("inter_x", None)


@pytest.mark.no_db
def test_turn_context_block_is_kept_out_of_the_research_query() -> None:
    """Deep Research collapses the prompt into one `input` string.

    The interactive /research path goes through the normal turn assembly, so the
    trailing context block is present in the message list. Letting it through
    would append the block verbatim to the question the model is asked to
    research.
    """
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    kwargs = client._build_deep_research_create_kwargs([
        UserMessage(content="Compare heat pump models for a cold climate."),
        UserMessage(
            content="<turn_context>\nCurrent time: 2026-07-25 10:00:00 UTC\n</turn_context>",
            is_turn_scaffolding=True,
        ),
    ])

    assert "Compare heat pump models" in str(kwargs["input"])
    assert "turn_context" not in str(kwargs["input"])
