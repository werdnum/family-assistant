"""Tests for InteractionsAgentProcessingService's submit/poll/cancel primitives."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.processing.interactions_agent_service import (
    InteractionsAgentProcessingService,
    TaintedSinkRefusedError,
)
from family_assistant.processing.protocol import (
    PENDING,
    DelegationPermanentError,
)
from family_assistant.processing.types import (
    ChatInteractionResult,
    ChatInteractionStatus,
    ProcessingServiceConfig,
)
from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyMode,
    TaintSource,
    TaintSourceType,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path

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


def _make_service(
    llm_client: GoogleGenAIClient,
    *,
    attachment_registry: AttachmentRegistry | None = None,
    taint_sink_class: SinkClass | None = None,
    taint_policy: TaintPolicyConfig | None = None,
) -> InteractionsAgentProcessingService:
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a research assistant for {user_name}."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="research",
        taint_sink_class=taint_sink_class,
    )
    return InteractionsAgentProcessingService(
        llm_client=llm_client,
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        attachment_registry=attachment_registry,
        taint_policy=taint_policy,
    )


def _google_client() -> GoogleGenAIClient:
    return GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")


@pytest.mark.asyncio
async def test_submit_async_mounts_attachments_into_the_sandbox(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A delegated attachment reaches the agent as a file, not as prompt text."""
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    db_context = Database(db_engine)
    attachment = await registry.register_user_attachment(
        db_context=db_context,
        content=b"a,b\n1,2\n",
        filename="figures.txt",
        mime_type="text/plain",
        user_id="user-1",
    )

    llm_client = _google_client()
    mock_interaction = AsyncMock()
    mock_interaction.id = "inter_att"
    llm_client.start_agent_interaction = AsyncMock(return_value=mock_interaction)
    service = _make_service(llm_client, attachment_registry=registry)

    await service.submit_async(
        [
            {"type": "text", "text": "Total column b."},
            {"type": "attachment", "attachment_id": attachment.attachment_id},
        ],
        conversation_id="conv-1",
        subconversation_id="sub-1",
        user_name="Andrew",
        db_context=db_context,
        acting_user_id="user-1",
    )

    sources = llm_client.start_agent_interaction.call_args.kwargs["environment_sources"]
    assert sources == [
        {
            "type": "inline",
            "content": base64.b64encode(b"a,b\n1,2\n").decode("ascii"),
            "encoding": "base64",
            "target": "/workspace/figures.txt",
        }
    ]


@pytest.mark.asyncio
async def test_attachments_sharing_a_filename_get_distinct_mount_targets(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Two files named the same must not collapse onto one sandbox path.

    The sandbox can hold one file per target, so a repeated target silently
    loses an input of a multi-file task.
    """
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    db_context = Database(db_engine)
    first = await registry.register_user_attachment(
        db_context=db_context,
        content=b"first",
        filename="data.txt",
        mime_type="text/plain",
        user_id="user-1",
    )
    second = await registry.register_user_attachment(
        db_context=db_context,
        content=b"second",
        filename="data.txt",
        mime_type="text/plain",
        user_id="user-1",
    )

    llm_client = _google_client()
    mock_interaction = AsyncMock()
    mock_interaction.id = "inter_two"
    llm_client.start_agent_interaction = AsyncMock(return_value=mock_interaction)
    service = _make_service(llm_client, attachment_registry=registry)

    await service.submit_async(
        [
            {"type": "attachment", "attachment_id": first.attachment_id},
            {"type": "attachment", "attachment_id": second.attachment_id},
        ],
        conversation_id="conv-1",
        subconversation_id="sub-1",
        user_name="Andrew",
        db_context=db_context,
        acting_user_id="user-1",
    )

    sources = llm_client.start_agent_interaction.call_args.kwargs["environment_sources"]
    targets = [source["target"] for source in sources]
    assert targets == ["/workspace/data.txt", "/workspace/data-2.txt"]


@pytest.mark.asyncio
async def test_submit_async_refuses_an_attachment_owned_by_another_user(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Routing is owner-scoped; another user's file is not mountable."""
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    db_context = Database(db_engine)
    stored = await registry.register_user_attachment(
        db_context=db_context,
        content=b"secret",
        filename="private.txt",
        mime_type="text/plain",
        user_id="owner",
    )
    # A second row over the same file, this time *owned*: an ownerless
    # attachment is visible to every actor by design, so only an owned one
    # exercises the scoping this routing relies on.
    attachment = await registry.register_attachment(
        db_context=db_context,
        attachment_id="att-owned",
        source_type="user",
        source_id="owner",
        mime_type="text/plain",
        description="Private file",
        size=len(b"secret"),
        storage_path=stored.storage_path,
        owner_user_id="owner",
        metadata={"original_filename": "private.txt"},
    )

    llm_client = _google_client()
    llm_client.start_agent_interaction = AsyncMock()
    service = _make_service(llm_client, attachment_registry=registry)

    with pytest.raises(DelegationPermanentError, match="belongs to"):
        await service.submit_async(
            [{"type": "attachment", "attachment_id": attachment.attachment_id}],
            conversation_id="conv-1",
            subconversation_id="sub-1",
            user_name="Mallory",
            db_context=db_context,
            acting_user_id="someone-else",
        )

    llm_client.start_agent_interaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_async_allows_a_confirmable_tier_already_gated_at_the_tool(
    db_engine: AsyncEngine,
) -> None:
    """An approved delegation must not be refused again at the profile gate.

    known_contact resolves to `confirm`, and `delegate_to_service` -- the only
    creator of a delegation run -- puts that to the user before the run exists.
    Refusing it here as well would fail every approved delegation.
    """
    llm_client = _google_client()
    mock_interaction = AsyncMock()
    mock_interaction.id = "inter_confirmed"
    llm_client.start_agent_interaction = AsyncMock(return_value=mock_interaction)
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    submission = await service.submit_async(
        [{"type": "text", "text": "Reformat the numbers Dana sent."}],
        conversation_id="conv-1",
        subconversation_id="sub-1",
        user_name="Andrew",
        db_context=Database(db_engine),
        initial_taint_sources=[
            TaintSource(
                source_type=TaintSourceType.EMAIL,
                source_id="msg-known",
                tier=SourceTrustTier.KNOWN_CONTACT,
                labels=frozenset(),
                reason="Mail from a known contact.",
            )
        ],
    )

    assert submission.remote_task_id == "inter_confirmed"


@pytest.mark.asyncio
async def test_a_direct_turn_refuses_a_confirmable_tier_no_gate_offered(
    db_engine: AsyncEngine,
) -> None:
    """A direct turn passed no tool gate, so `confirm` has nobody behind it."""
    llm_client = _google_client()
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    result = await service.handle_chat_interaction(
        db_context=Database(db_engine),
        interface_type="web",
        conversation_id="conv-1",
        trigger_content_parts=[{"type": "text", "text": "Run this."}],
        trigger_interface_message_id=None,
        user_name="Andrew",
        initial_taint_sources=[
            TaintSource(
                source_type=TaintSourceType.EMAIL,
                source_id="msg-known",
                tier=SourceTrustTier.KNOWN_CONTACT,
                labels=frozenset(),
                reason="Mail from a known contact.",
            )
        ],
    )

    assert result.status is ChatInteractionStatus.ERROR
    assert "known_contact" in result.text_reply


@pytest.mark.asyncio
async def test_the_streaming_entry_point_is_gated_too(
    db_engine: AsyncEngine,
) -> None:
    """The web path streams, so a gate only on the non-streaming call is no gate."""
    llm_client = _google_client()
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    events = [
        event
        async for event in service.handle_chat_interaction_stream(
            db_context=Database(db_engine),
            interface_type="web",
            conversation_id="conv-1",
            trigger_content_parts=[{"type": "text", "text": "Run this."}],
            trigger_interface_message_id=None,
            user_name="Andrew",
            initial_taint_sources=[
                TaintSource(
                    source_type=TaintSourceType.EMAIL,
                    source_id="msg-1",
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason="Inbound email.",
                )
            ],
        )
    ]

    assert [event.type for event in events] == ["error"]
    assert "unknown_external" in (events[0].error or "")


@pytest.mark.asyncio
async def test_submit_async_denies_a_sandbox_profile_untrusted_content(
    db_engine: AsyncEngine,
) -> None:
    """Email-derived instructions cannot direct a code-execution agent.

    The shipped matrix maps unknown_external -> sandbox_network to deny, so a
    profile that declares that sink refuses the run rather than submitting it.
    """
    llm_client = _google_client()
    llm_client.start_agent_interaction = AsyncMock()
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    with pytest.raises(TaintedSinkRefusedError, match="unknown_external"):
        await service.submit_async(
            [{"type": "text", "text": "Run the script this email describes."}],
            conversation_id="conv-1",
            subconversation_id="sub-1",
            user_name="Andrew",
            db_context=Database(db_engine),
            initial_taint_sources=[
                TaintSource(
                    source_type=TaintSourceType.EMAIL,
                    source_id="msg-1",
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason="Inbound email.",
                )
            ],
        )

    llm_client.start_agent_interaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_async_allows_a_sandbox_profile_trusted_content(
    db_engine: AsyncEngine,
) -> None:
    """The user's own request reaches the agent untouched."""
    llm_client = _google_client()
    mock_interaction = AsyncMock()
    mock_interaction.id = "inter_ok"
    llm_client.start_agent_interaction = AsyncMock(return_value=mock_interaction)
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    submission = await service.submit_async(
        [{"type": "text", "text": "Write a script to total this column."}],
        conversation_id="conv-1",
        subconversation_id="sub-1",
        user_name="Andrew",
        db_context=Database(db_engine),
    )

    assert submission.remote_task_id == "inter_ok"


@pytest.mark.asyncio
async def test_an_untrusted_attachment_denies_the_run_it_was_routed_into(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """The routed file's own provenance counts toward the gate, not just the text.

    This is what makes routing safe rather than merely possible: a trusted
    request carrying an untrusted file is still an untrusted turn.
    """
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    db_context = Database(db_engine)
    attachment = await registry.register_user_attachment(
        db_context=db_context,
        content=b"payload",
        filename="invoice.txt",
        mime_type="text/plain",
        user_id="user-1",
    )
    # A second row over the same stored file, labelled the way the email
    # intake path labels artifacts it creates from untrusted mail.
    stored_path = registry.get_attachment_path(
        attachment.attachment_id,
        stored_path=attachment.storage_path,
        source_type="user",
    )
    assert stored_path is not None
    tainted = await registry.register_attachment(
        db_context=db_context,
        attachment_id="att-from-email",
        source_type="email",
        source_id="<msg-1@example.com>",
        mime_type="text/plain",
        description="Invoice from an unknown sender",
        size=len(b"payload"),
        storage_path=stored_path.as_posix(),
        owner_user_id="user-1",
        metadata={
            "original_filename": "invoice.txt",
            "source_trust_tier": "unknown_external",
        },
    )

    llm_client = _google_client()
    llm_client.start_agent_interaction = AsyncMock()
    service = _make_service(
        llm_client,
        attachment_registry=registry,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    with pytest.raises(TaintedSinkRefusedError, match="unknown_external"):
        await service.submit_async(
            [
                {"type": "text", "text": "Total column b."},
                {"type": "attachment", "attachment_id": tainted.attachment_id},
            ],
            conversation_id="conv-1",
            subconversation_id="sub-1",
            user_name="Andrew",
            db_context=db_context,
            acting_user_id="user-1",
        )

    llm_client.start_agent_interaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_async_renders_prompt_and_starts_interaction(
    db_engine: AsyncEngine,
) -> None:
    """submit_async renders the system prompt and forwards content as input."""
    llm_client = _google_client()
    mock_interaction = AsyncMock()
    mock_interaction.id = "inter_new"
    llm_client.start_agent_interaction = AsyncMock(return_value=mock_interaction)
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

    call_args = llm_client.start_agent_interaction.call_args
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
    llm_client.start_agent_interaction = AsyncMock(return_value=mock_interaction)
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

    call_args = llm_client.start_agent_interaction.call_args
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

    kwargs = client._build_agent_create_kwargs([
        UserMessage(content="Compare heat pump models for a cold climate."),
        UserMessage(
            content="<turn_context>\nCurrent time: 2026-07-25 10:00:00 UTC\n</turn_context>",
            is_turn_scaffolding=True,
        ),
    ])

    assert "Compare heat pump models" in str(kwargs["input"])
    assert "turn_context" not in str(kwargs["input"])


@pytest.mark.no_db
def test_deep_research_prompt_carries_the_clock() -> None:
    """No turn-context block survives to Deep Research, so the prompt must.

    Research grounded on live web results is the case that most needs a date;
    dropping the block without folding the time in would leave "the latest on X
    this week" unanswerable.
    """
    service = _make_service(
        GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")
    )

    prompt = service.format_system_prompt(user_name="tester")

    assert "Current time:" in prompt
