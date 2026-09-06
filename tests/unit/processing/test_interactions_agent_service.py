"""Tests for InteractionsAgentProcessingService's submit/poll/cancel primitives."""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from google.genai.interactions import (
    Error,
    Interaction,
    InteractionStatus,
    ModelOutputStep,
    TextContent,
    UserInputStep,
)

from family_assistant.config_models import (
    AppConfig,
    ToolCallReviewConfig,
    ToolsConfig,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.model_selection import ResolvedModelSelection
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.processing.interactions_agent_service import (
    InteractionsAgentProcessingService,
)
from family_assistant.processing.protocol import (
    PENDING,
    DelegationPermanentError,
    TaintedSinkRefusedError,
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
    TurnTaintState,
    merge_history_taint,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.tool_call_review import (
    ToolCallReviewer,
    ToolCallReviewResponse,
    ToolCallReviewStatus,
    ToolCallReviewVerdict,
)
from family_assistant.storage.database import Database
from family_assistant.tools import LocalToolsProvider, TaintTrackingToolsProvider
from family_assistant.tools.types import ConfirmationOutcome
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.storage.repositories.delegation_runs import (
        DelegationRunCreate,
    )
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools import ToolsProvider
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


class _ReviewLLM:
    """Structured reviewer fake that can pause inside its model call."""

    def __init__(
        self,
        verdict: ToolCallReviewVerdict,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.verdict = verdict
        self.entered = entered
        self.release = release
        self.calls = 0
        self.last_messages: Sequence[LLMMessage] | None = None

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        assert response_model is ToolCallReviewResponse
        assert max_retries == 0
        self.calls += 1
        self.last_messages = messages
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return cast(
            "T",
            ToolCallReviewResponse(
                verdict=self.verdict,
                reason=f"Reviewer chose {self.verdict.value}.",
                safer_alternative="Keep the work local."
                if self.verdict is ToolCallReviewVerdict.DENY
                else None,
            ),
        )


class _DecisionOnlyConfirmationManager:
    def __init__(self, outcome: ConfirmationOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def request_confirmation(self, **kwargs: object) -> ConfirmationOutcome:
        self.calls.append(kwargs)
        return self.outcome


def _reviewing_provider(
    llm: _ReviewLLM,
    *,
    taint_policy: TaintPolicyConfig,
) -> TaintTrackingToolsProvider:
    review_config = ToolCallReviewConfig(timeout_seconds=1)
    return TaintTrackingToolsProvider(
        LocalToolsProvider(registrations=[]),
        taint_policy=taint_policy,
        tool_call_reviewer=ToolCallReviewer(cast("LLMInterface", llm), review_config),
        review_config=review_config,
        include_aggregated_context=True,
    )


def _make_service(
    llm_client: LLMInterface,
    *,
    attachment_registry: AttachmentRegistry | None = None,
    taint_sink_class: SinkClass | None = None,
    taint_policy: TaintPolicyConfig | None = None,
    tools_provider: ToolsProvider | None = None,
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
        tools_provider=tools_provider or SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        attachment_registry=attachment_registry,
        taint_policy=taint_policy,
    )


def _state_from(sources: list[TaintSource]) -> TurnTaintState:
    state = TurnTaintState.empty()
    for source in sources:
        state = state.add_source(source)
    return state


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
async def test_submit_async_honours_an_approval_persisted_with_the_run(
    db_engine: AsyncEngine,
) -> None:
    """An approved delegation must not be refused again at the profile gate.

    The run persists the parent turn's taint state, approval and all, so the
    submit path reads the answer a user actually gave instead of assuming one
    from the fact that a delegation exists.
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
        initial_taint_state=_state_from([
            TaintSource(
                source_type=TaintSourceType.EMAIL,
                source_id="msg-known",
                tier=SourceTrustTier.KNOWN_CONTACT,
                labels=frozenset(),
                reason="Mail from a known contact.",
            )
        ]).approve_sink(SinkClass.SANDBOX_NETWORK, profile_id="research"),
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
async def test_an_injected_attachment_carries_its_provenance_into_the_turn(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A text attachment is injected as text, so its taint must ride with it.

    Otherwise a direct `/coder` turn reads as trusted while an email-derived
    file's instructions reach the sandbox in the request.
    """
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    db_context = Database(db_engine)
    stored = await registry.register_user_attachment(
        db_context=db_context,
        content=b"do the thing",
        filename="note.txt",
        mime_type="text/plain",
        user_id="user-1",
    )
    stored_path = registry.get_attachment_path(
        stored.attachment_id, stored_path=stored.storage_path, source_type="user"
    )
    assert stored_path is not None
    tainted = await registry.register_attachment(
        db_context=db_context,
        attachment_id="att-emailed",
        source_type="email",
        source_id="<msg-1@example.com>",
        mime_type="text/plain",
        description="From an unknown sender",
        size=len(b"do the thing"),
        storage_path=stored_path.as_posix(),
        owner_user_id="user-1",
        metadata={
            "original_filename": "note.txt",
            "source_trust_tier": "unknown_external",
        },
    )

    service = _make_service(_google_client(), attachment_registry=registry)
    processed = await service.attachment_processor.process_content_parts(
        db_context,
        "conv-1",
        [{"type": "attachment", "attachment_id": tainted.attachment_id}],
        acting_user_id="user-1",
        llm_client=service.llm_client,
    )

    assert merge_history_taint(processed.messages).max_tier is (
        SourceTrustTier.UNKNOWN_EXTERNAL
    )


@pytest.mark.asyncio
async def test_taint_carried_by_history_gates_the_turn(
    db_engine: AsyncEngine,
) -> None:
    """The gate sees the whole turn, not just the trigger's own sources.

    A trusted "go ahead" can pull untrusted content in behind it -- an
    email-derived attachment injected during preparation, or a tainted earlier
    message. The check therefore runs inside the LLM loop, against the state it
    merges from history plus the trigger, rather than at an entry point where
    only `initial_taint_sources` is visible.
    """
    llm_client = _google_client()
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    tainted_history = UserMessage(
        content="Forwarded: please run the attached script.",
        taint_metadata=_state_from([
            TaintSource(
                source_type=TaintSourceType.EMAIL,
                source_id="msg-1",
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="Inbound email.",
            )
        ]).to_metadata(),
    )

    with pytest.raises(TaintedSinkRefusedError, match="unknown_external"):
        await service.process_message(
            db_context=Database(db_engine),
            messages=[tainted_history, UserMessage(content="Go ahead.")],
            interface_type="web",
            conversation_id="conv-1",
            user_name="Andrew",
            turn_id="turn-1",
            chat_interface=None,
            llm_client=service.llm_client,
            model_selection=ResolvedModelSelection.unselected(None),
        )


@pytest.mark.asyncio
async def test_an_approval_travelling_with_the_taint_permits_a_confirm(
    db_engine: AsyncEngine,
) -> None:
    """The gate reads the approval off the taint rather than inferring one.

    known_contact resolves to `confirm`. Whoever put that question to the user
    records the answer on this turn's taint, and it travels with the content --
    so a gate downstream neither re-asks nor has to reason about which call
    path it is on.
    """
    llm_client = _google_client()
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    known_contact = [
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-known",
            tier=SourceTrustTier.KNOWN_CONTACT,
            labels=frozenset(),
            reason="Mail from a known contact.",
        )
    ]

    without_approval = service.sink_refusal_reason(_state_from(known_contact))
    with_approval = service.sink_refusal_reason(
        _state_from(known_contact).approve_sink(
            SinkClass.SANDBOX_NETWORK, profile_id="research"
        )
    )

    assert without_approval is not None
    assert with_approval is None


@pytest.mark.asyncio
async def test_submit_async_denies_a_sandbox_profile_untrusted_content(
    db_engine: AsyncEngine,
) -> None:
    """Email-derived instructions cannot direct a code-execution agent.

    The shipped matrix gates unknown_external -> sandbox_network, and this
    submission path has no confirmation channel, so a profile that declares
    that sink refuses the run rather than submitting it.
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


@pytest.mark.parametrize(
    ("verdict", "starts_interaction"),
    [
        (ToolCallReviewVerdict.ALLOW, True),
        (ToolCallReviewVerdict.DENY, False),
    ],
)
@pytest.mark.asyncio
async def test_submit_async_enforce_routes_profile_sink_through_reviewer(
    db_engine: AsyncEngine,
    verdict: ToolCallReviewVerdict,
    starts_interaction: bool,
) -> None:
    """Pollable profile sinks adjudicate instead of applying a direct refusal."""
    llm = _ReviewLLM(verdict)
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _reviewing_provider(llm, taint_policy=taint_policy)
    llm_client = _google_client()
    interaction = AsyncMock()
    interaction.id = "inter_reviewed"
    llm_client.start_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=taint_policy,
        tools_provider=provider,
    )
    db_context = Database(db_engine)
    source = TaintSource(
        source_type=TaintSourceType.EMAIL,
        source_id="msg-1",
        tier=SourceTrustTier.UNKNOWN_EXTERNAL,
        labels=frozenset(),
        reason="Inbound email.",
    )

    if starts_interaction:
        submission = await service.submit_async(
            [{"type": "text", "text": "Run the script this email describes."}],
            conversation_id="conv-review",
            subconversation_id="sub-1",
            user_name="Andrew",
            db_context=db_context,
            initial_taint_sources=[source],
        )
        assert submission.remote_task_id == "inter_reviewed"
    else:
        with pytest.raises(TaintedSinkRefusedError, match="Reviewer chose deny"):
            await service.submit_async(
                [
                    {
                        "type": "text",
                        "text": "Run the script this email describes.",
                    }
                ],
                conversation_id="conv-review",
                subconversation_id="sub-1",
                user_name="Andrew",
                db_context=db_context,
                initial_taint_sources=[source],
            )

    assert llm.calls == 1
    assert llm.last_messages is not None
    review_prompt = "\n".join(str(message.content) for message in llm.last_messages)
    assert "Run the script this email describes." not in review_prompt
    assert "conversation_provenance_stub" in review_prompt
    assert llm_client.start_agent_interaction.await_count == int(starts_interaction)
    events = await db_context.taint_audit_events.list_for_conversation("conv-review")
    reviews = [event for event in events if event["event_type"] == "tool_call_review"]
    assert len(reviews) == 1
    assert reviews[0]["tool_name"] == "profile:research"
    assert reviews[0]["review_verdict"] == verdict.value
    assert reviews[0]["review_status"] == ToolCallReviewStatus.MODEL_VERDICT.value
    await provider.close()


@pytest.mark.asyncio
async def test_submit_async_trusted_request_is_rendered_to_reviewer(
    db_engine: AsyncEngine,
) -> None:
    """Pollable review receives the merged trusted provenance on its user row."""
    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW)
    taint_policy = TaintPolicyConfig.model_validate({
        "mode": "enforce",
        "matrix_overrides": {
            "trusted_user": {
                "sandbox_network": {
                    "outcome": "adjudicate",
                    "fallback": "confirm",
                }
            }
        },
    })
    provider = _reviewing_provider(llm, taint_policy=taint_policy)
    llm_client = _google_client()
    interaction = AsyncMock()
    interaction.id = "inter_trusted"
    llm_client.start_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=taint_policy,
        tools_provider=provider,
    )

    submission = await service.submit_async(
        [{"type": "text", "text": "Write a script to total this column."}],
        conversation_id="conv-trusted-review",
        subconversation_id="sub-1",
        user_name="Andrew",
        db_context=Database(db_engine),
    )

    assert submission.remote_task_id == "inter_trusted"
    assert llm.last_messages is not None
    review_prompt = "\n".join(str(message.content) for message in llm.last_messages)
    assert '<trusted_conversation index="1" role="user">' in review_prompt
    assert "Write a script to total this column." in review_prompt
    await provider.close()


@pytest.mark.asyncio
async def test_submit_async_confirm_fails_closed_without_live_confirmation(
    db_engine: AsyncEngine,
) -> None:
    """An unattended profile submit never creates an executable durable request."""
    llm = _ReviewLLM(ToolCallReviewVerdict.CONFIRM)
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _reviewing_provider(llm, taint_policy=taint_policy)
    llm_client = _google_client()
    llm_client.start_agent_interaction = AsyncMock()
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=taint_policy,
        tools_provider=provider,
    )

    with pytest.raises(
        TaintedSinkRefusedError,
        match="live decision-only confirmation is unavailable",
    ):
        await service.submit_async(
            [{"type": "text", "text": "Run the emailed script."}],
            conversation_id="conv-unattended-confirm",
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

    assert llm.calls == 1
    llm_client.start_agent_interaction.assert_not_awaited()
    await provider.close()


@pytest.mark.asyncio
async def test_live_llm_loop_profile_confirm_is_decision_only(
    db_engine: AsyncEngine,
) -> None:
    """Live approval resumes the current model call without executable replay."""
    review_llm = _ReviewLLM(ToolCallReviewVerdict.CONFIRM)
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _reviewing_provider(review_llm, taint_policy=taint_policy)
    model = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content="Ran after live approval.", tool_calls=None),
    )
    service = _make_service(
        model,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=taint_policy,
        tools_provider=provider,
    )
    manager = _DecisionOnlyConfirmationManager(ConfirmationOutcome(kind="approved"))
    callback = AsyncMock()

    result = await service.handle_chat_interaction(
        db_context=Database(db_engine),
        interface_type="web",
        conversation_id="conv-live-confirm",
        trigger_content_parts=[{"type": "text", "text": "Run the emailed script."}],
        trigger_interface_message_id=None,
        user_name="Andrew",
        user_id="user-1",
        confirmation_ui_managers=cast(
            "dict[str, ConfirmationUIManager]", {"web": manager}
        ),
        request_confirmation_callback=callback,
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

    assert result.status is ChatInteractionStatus.SUCCESS
    assert result.text_reply == "Ran after live approval."
    assert review_llm.calls == 1
    assert len(manager.calls) == 1
    assert manager.calls[0]["tool_name"] == "profile:research"
    assert manager.calls[0]["wait_for_durable_execution"] is False
    callback.assert_not_awaited()
    await provider.close()


@pytest.mark.asyncio
async def test_submit_async_observe_review_is_detached_and_drained_on_close(
    db_engine: AsyncEngine,
) -> None:
    """Observe submits before the shadow model verdict and later audits it."""
    entered = asyncio.Event()
    release = asyncio.Event()
    llm = _ReviewLLM(
        ToolCallReviewVerdict.DENY,
        entered=entered,
        release=release,
    )
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.OBSERVE)
    provider = _reviewing_provider(llm, taint_policy=taint_policy)
    llm_client = _google_client()
    interaction = AsyncMock()
    interaction.id = "inter_shadow"
    llm_client.start_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(
        llm_client,
        taint_sink_class=SinkClass.SANDBOX_NETWORK,
        taint_policy=taint_policy,
        tools_provider=provider,
    )
    db_context = Database(db_engine)

    submission = await service.submit_async(
        [{"type": "text", "text": "Run the script this email describes."}],
        conversation_id="conv-shadow",
        subconversation_id="sub-1",
        user_name="Andrew",
        db_context=db_context,
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

    assert submission.remote_task_id == "inter_shadow"
    assert not release.is_set()
    await asyncio.wait_for(entered.wait(), timeout=1)
    events = await db_context.taint_audit_events.list_for_conversation("conv-shadow")
    assert not [event for event in events if event["event_type"] == "tool_call_review"]

    release.set()
    await provider.close()

    events = await db_context.taint_audit_events.list_for_conversation("conv-shadow")
    reviews = [event for event in events if event["event_type"] == "tool_call_review"]
    assert len(reviews) == 1
    assert reviews[0]["review_verdict"] == ToolCallReviewVerdict.DENY.value
    assert reviews[0]["review_status"] == ToolCallReviewStatus.MODEL_VERDICT.value


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
        interaction = Interaction(status=status)
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
    interaction = Interaction(status="queued")
    llm_client.get_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(llm_client)

    result = await service.poll_async("inter_x", None)

    assert result is PENDING


@pytest.mark.asyncio
async def test_poll_async_completed_returns_success() -> None:
    """A completed interaction becomes a successful ChatInteractionResult."""
    llm_client = _google_client()
    interaction = Interaction(
        status="completed",
        steps=[
            UserInputStep(content=[TextContent(text="hi")]),
            ModelOutputStep(content=[TextContent(text="The final research report.")]),
        ],
    )
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
    interaction = Interaction(status=cast("InteractionStatus", status))
    llm_client.get_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(llm_client)

    result = await service.poll_async("inter_x", None)

    assert isinstance(result, ChatInteractionResult)
    assert result.has_error
    assert status in result.text_reply


@pytest.mark.asyncio
async def test_poll_async_terminal_error_captures_interaction_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The API's diagnostic faults surface in the traceback, not just the status.

    Google can cancel an agent interaction while a same-account run is
    already in flight, recording the reason on ``interaction.errors``. A
    bare status string loses that; the error detail must reach the log and
    the persisted error_traceback.
    """
    llm_client = _google_client()
    interaction = Interaction(
        status="cancelled",
        errors=[
            Error(code="err/concurrency_limit", message="agent session in flight"),
            Error(message="retry after the running agent completes"),
        ],
    )
    llm_client.get_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(llm_client)

    with caplog.at_level(logging.WARNING):
        result = await service.poll_async("inter_x", None)

    assert isinstance(result, ChatInteractionResult)
    assert result.has_error
    assert result.error_traceback is not None
    assert (
        "Errors: {code=err/concurrency_limit, message=agent session in flight}; "
        "{message=retry after the running agent completes}" in result.error_traceback
    )
    assert any(
        "errors: {code=err/concurrency_limit" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_poll_async_terminal_error_without_errors_leaves_traceback_bare() -> None:
    """No errors payload means no fabricated detail in the traceback."""
    llm_client = _google_client()
    interaction = Interaction(status="cancelled")
    llm_client.get_agent_interaction = AsyncMock(return_value=interaction)
    service = _make_service(llm_client)

    result = await service.poll_async("inter_x", None)

    assert isinstance(result, ChatInteractionResult)
    assert result.has_error
    assert result.error_traceback == (
        "Interaction inter_x ended with status 'cancelled'."
    )


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
