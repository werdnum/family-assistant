from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.a2a.remote_service import RemoteA2AService
from family_assistant.a2a.types import (
    Artifact,
    Part,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - the smallest helper that applies the synthetic self-delegation allow
)
from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.llm.model_selection import (
    ModelSelectionRequest,
    ModelTierEligibility,
    ModelTierNotPermitted,
    ModelTierOption,
    ResolvedModelSelection,
    resolve_model_selection,
)
from family_assistant.processing.types import (
    ChatInteractionResult,
    ChatInteractionStatus,
    DelegationSecurityLevel,
    RemoteServiceConfig,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
)
from family_assistant.storage.database import Database
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.confirmation import MAX_DELEGATION_REQUEST_CHARS
from family_assistant.tools.policy import ToolPolicyDecision
from family_assistant.tools.services import (
    _confirmation_tool_arguments,  # noqa: PLC2701 - the drift these guard is between two private helpers
    _durable_authorization_matches,  # noqa: PLC2701 - see above
    delegate_to_service_tool,
)
from family_assistant.tools.types import (
    ToolArguments,
    ToolConfirmationAuthorization,
    ToolExecutionContext,
    ToolResult,
)
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.a2a.client import A2AClientWrapper
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.processing import ProcessingService
    from family_assistant.tools import ToolDescriptor


class _Namespace:
    """Small attribute bag for test service doubles.

    Carries the pinned tier eligibility unless a double states its own, so a
    ``service_config`` that says nothing about tiers is what the real gate sees
    for a profile with an inline model.
    """

    service_config: Any
    tier_eligibility: ModelTierEligibility = ModelTierEligibility()

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401 - test helper
        self.__dict__.update(kwargs)


class _SynchronousRemoteClient:
    """Minimal remote client that completes inline without network access."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_message(
        self,
        content_parts: list[ContentPartDict],
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, object] | None = None,
        acting_user_id: str | None = None,
    ) -> Task:
        _ = content_parts
        _ = task_id
        _ = metadata
        _ = acting_user_id
        self.calls += 1
        return Task(
            id="synchronous-remote-task",
            context_id=context_id or "synchronous-remote-context",
            status=TaskStatus(state=TaskState.completed),
            artifacts=[
                Artifact(
                    artifact_id="synchronous-remote-artifact",
                    parts=[Part(root=TextPart(text="remote delegated"))],
                )
            ],
        )


def _unknown_external_tracker() -> InMemoryTurnTaintTracker:
    tracker = InMemoryTurnTaintTracker()
    tracker.add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="42",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({"source_unknown_external"}),
            reason="test email source",
        )
    )
    return tracker


def _db_without_history() -> Database:
    """A database whose turn history is empty.

    Delegation reads the delegating turn for the human request behind the goal.
    These tests exercise policy and plumbing, not that lookup, so the read is
    real enough to be awaited and returns nothing.
    """
    db = MagicMock(spec=Database)
    db.message_history = MagicMock()
    db.message_history.get_by_turn_id = AsyncMock(return_value=[])
    return cast("Database", db)


@pytest.mark.asyncio
async def test_delegate_to_service_blocks_disallowed_source_profile() -> None:
    target_service = _Namespace(
        service_config=_Namespace(
            id="target_profile",
            allowed_delegation_sources=["other_profile"],
        ),
    )
    source_service = _Namespace(
        service_config=_Namespace(
            id="source_profile",
            tools_config=_Namespace(confirmation_timeout_seconds=10.0),
        ),
        processing_services_registry={"target_profile": target_service},
    )

    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id=None,
        db_context=_db_without_history(),
        processing_service=cast("ProcessingService", source_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="do work",
    )

    assert isinstance(result, ToolResult)
    assert result.text is not None
    assert "source_profile" in result.text
    assert "not permitted to delegate" in result.text


@pytest.mark.asyncio
async def test_delegate_to_service_refuses_over_length_request_when_confirming() -> (
    None
):
    target_handler = AsyncMock()
    target_service = _Namespace(
        service_config=_Namespace(
            id="target_profile",
            allowed_delegation_sources=None,
        ),
        handle_chat_interaction=target_handler,
    )
    source_service = _Namespace(
        service_config=_Namespace(
            id="source_profile",
            tools_config=_Namespace(confirmation_timeout_seconds=10.0),
        ),
        processing_services_registry={"target_profile": target_service},
    )

    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id=None,
        db_context=_db_without_history(),
        processing_service=cast("ProcessingService", source_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    over_limit = "x" * (MAX_DELEGATION_REQUEST_CHARS + 1)
    # confirm_delegation=True means this hand-off will be approved against a
    # confirmation prompt, so the over-long request must be refused.
    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request=over_limit,
        confirm_delegation=True,
    )

    assert isinstance(result, ToolResult)
    assert result.text is not None
    assert str(MAX_DELEGATION_REQUEST_CHARS) in result.text
    assert "exceeds" in result.text
    # The over-long request must never reach the target profile.
    target_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_synchronous_delegate_to_service_passes_parent_taint_sources() -> None:
    target_handler = AsyncMock(
        return_value=ChatInteractionResult(
            status=ChatInteractionStatus.SUCCESS,
            text_reply="delegated",
        )
    )
    target_service = _Namespace(
        service_config=_Namespace(
            id="target_profile",
            allowed_delegation_sources=None,
        ),
        handle_chat_interaction=target_handler,
    )
    source_service = _Namespace(
        service_config=_Namespace(
            id="source_profile",
            tools_config=ToolsConfig(async_delegation_enabled=False),
        ),
        processing_services_registry={"target_profile": target_service},
    )
    tracker = _unknown_external_tracker()

    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id="turn-1",
        db_context=_db_without_history(),
        processing_service=cast("ProcessingService", source_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        taint_tracker=tracker,
        credential_resolvers=None,
        api_backend=None,
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="summarize this email",
    )

    assert result.text == "delegated"
    target_handler.assert_awaited_once()
    await_args = target_handler.await_args
    assert await_args is not None
    initial_sources = await_args.kwargs["initial_taint_sources"]
    assert len(initial_sources) == 1
    assert initial_sources[0].tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert initial_sources[0].source_type is TaintSourceType.EMAIL


@pytest.mark.asyncio
async def test_synchronous_remote_delegation_accepts_review_trigger() -> None:
    """Remote targets accept the shared review-trigger keyword without TypeError."""
    remote_client = _SynchronousRemoteClient()
    target_service = RemoteA2AService(
        service_config=RemoteServiceConfig(
            id="remote_profile",
            description="Remote test profile",
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        client=cast("A2AClientWrapper", remote_client),
    )
    source_service = _Namespace(
        service_config=_Namespace(
            id="source_profile",
            tools_config=ToolsConfig(async_delegation_enabled=False),
        ),
        processing_services_registry={"remote_profile": target_service},
    )
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id="turn-remote-sync",
        db_context=_db_without_history(),
        processing_service=cast("ProcessingService", source_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="remote_profile",
        user_request="complete this remotely",
    )

    assert result.get_text() == "remote delegated"
    assert remote_client.calls == 1


@pytest.mark.asyncio
async def test_async_delegate_to_service_persists_parent_taint_state(
    db_engine: AsyncEngine,
) -> None:
    target_service = _Namespace(
        service_config=_Namespace(
            id="target_profile",
            allowed_delegation_sources=None,
        ),
    )
    source_service = _Namespace(
        service_config=_Namespace(
            id="source_profile",
            tools_config=ToolsConfig(async_delegation_enabled=True),
        ),
        processing_services_registry={"target_profile": target_service},
    )
    tracker = _unknown_external_tracker()

    db_context = Database(db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id="turn-1",
        db_context=db_context,
        processing_service=cast("ProcessingService", source_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        taint_tracker=tracker,
        credential_resolvers=None,
        api_backend=None,
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="summarize this email",
        delivery_hint="background",
    )

    assert result.data is not None
    result_data = cast("dict[str, object]", result.data)
    delegation_id = result_data["delegation_id"]
    assert isinstance(delegation_id, str)
    run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)

    assert run is not None
    taint_state = run["taint_state_json"]
    assert taint_state is not None
    assert taint_state.get("max_tier") == "unknown_external"
    sources = taint_state.get("sources")
    assert isinstance(sources, list)
    assert sources[0]["source_type"] == "email"


def _tiered_target(handler: AsyncMock) -> _Namespace:
    """A delegation target admitting `deep` automatically but not `frontier`."""
    return _Namespace(
        service_config=_Namespace(
            id="target_profile",
            allowed_delegation_sources=None,
            tier_eligibility=ModelTierEligibility(
                default_tier="standard",
                selectable=(
                    ModelTierOption(id="standard", label="Standard"),
                    ModelTierOption(id="deep", label="Deep"),
                    ModelTierOption(id="frontier", label="Max"),
                ),
                auto=frozenset({"standard", "deep"}),
            ),
        ),
        handle_chat_interaction=handler,
    )


def _delegating_context(
    target: _Namespace,
    *,
    db_context: Database,
    async_delegation_enabled: bool,
) -> ToolExecutionContext:
    source_service = _Namespace(
        service_config=_Namespace(
            id="source_profile",
            tools_config=ToolsConfig(async_delegation_enabled=async_delegation_enabled),
        ),
        processing_services_registry={"target_profile": target},
    )
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id="turn-1",
        db_context=db_context,
        processing_service=cast("ProcessingService", source_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_delegating_at_an_automatically_admitted_tier_runs_on_it() -> None:
    handler = AsyncMock(
        return_value=ChatInteractionResult(
            status=ChatInteractionStatus.SUCCESS, text_reply="delegated"
        )
    )
    context = _delegating_context(
        _tiered_target(handler),
        db_context=_db_without_history(),
        async_delegation_enabled=False,
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="think hard about this",
        model_tier="deep",
    )

    assert result.text == "delegated"
    await_args = handler.await_args
    assert await_args is not None
    selection = await_args.kwargs["model_selection"]
    assert selection == ResolvedModelSelection(
        tier="deep", requested="deep", source="model"
    )


@pytest.mark.asyncio
async def test_delegating_at_a_tier_only_a_user_may_choose_is_refused() -> None:
    """A model cannot spend its way past what the target admits from it."""
    handler = AsyncMock()
    context = _delegating_context(
        _tiered_target(handler),
        db_context=_db_without_history(),
        async_delegation_enabled=False,
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="think hard about this",
        model_tier="frontier",
    )

    assert result.text is not None
    assert result.text.startswith("Error:")
    assert "frontier" in result.text
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_delegation_naming_no_tier_runs_on_the_targets_default() -> None:
    """A parent's own tier never propagates: the default is the absence of one."""
    handler = AsyncMock(
        return_value=ChatInteractionResult(
            status=ChatInteractionStatus.SUCCESS, text_reply="delegated"
        )
    )
    context = _delegating_context(
        _tiered_target(handler),
        db_context=_db_without_history(),
        async_delegation_enabled=False,
    )

    await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="do the thing",
    )

    await_args = handler.await_args
    assert await_args is not None
    selection = await_args.kwargs["model_selection"]
    assert selection.tier == "standard"
    assert selection.source == "default"
    assert selection.requested is None


@pytest.mark.asyncio
async def test_a_queued_delegation_persists_its_resolved_tier(
    db_engine: AsyncEngine,
) -> None:
    """The envelope is frozen at enqueue, not re-resolved when a worker runs it."""
    db_context = Database(db_engine)
    context = _delegating_context(
        _tiered_target(AsyncMock()),
        db_context=db_context,
        async_delegation_enabled=True,
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="think hard about this",
        model_tier="deep",
        delivery_hint="background",
    )

    result_data = cast("dict[str, object]", result.data)
    delegation_id = cast("str", result_data["delegation_id"])
    run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)

    assert run is not None
    persisted = run["model_selection_json"]
    assert persisted is not None
    assert ResolvedModelSelection.from_json(persisted) == ResolvedModelSelection(
        tier="deep", requested="deep", source="model"
    )


@pytest.mark.asyncio
async def test_a_refused_tier_never_becomes_a_queued_run(
    db_engine: AsyncEngine,
) -> None:
    db_context = Database(db_engine)
    context = _delegating_context(
        _tiered_target(AsyncMock()),
        db_context=db_context,
        async_delegation_enabled=True,
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="think hard about this",
        model_tier="frontier",
        delivery_hint="background",
    )

    assert result.data is None
    assert (
        await db_context.delegation_runs.list_for_conversation(
            conversation_id="conversation"
        )
        == []
    )


def _stored_approval(**arguments: object) -> ToolConfirmationAuthorization:
    return ToolConfirmationAuthorization(
        tool_name="delegate_to_service",
        call_id="delegate_to_service_1",
        tool_args=cast("ToolArguments", arguments),
    )


def test_a_stored_approval_records_the_tier_it_authorized() -> None:
    """The stored args and the effective args must not drift apart.

    `_durable_authorization_matches` requires every stored argument to be
    present and equal, so an argument added to one side and not the other
    silently stops every stored approval from matching.
    """
    stored = _confirmation_tool_arguments(
        target_service_id="target_profile",
        user_request="think hard",
        confirm_delegation=True,
        attachment_ids=None,
        resume_delegation_id=None,
        model_tier="deep",
    )

    assert stored["model_tier"] == "deep"
    assert _durable_authorization_matches(
        _stored_approval(**stored),
        {
            "target_service_id": "target_profile",
            "user_request": "think hard",
            "confirm_delegation": True,
            "model_tier": "deep",
        },
    )


def test_an_approval_for_one_tier_does_not_authorize_another() -> None:
    stored = _stored_approval(
        target_service_id="target_profile",
        user_request="think hard",
        confirm_delegation=True,
        model_tier="deep",
    )

    assert not _durable_authorization_matches(
        stored,
        {
            "target_service_id": "target_profile",
            "user_request": "think hard",
            "confirm_delegation": True,
            "model_tier": "frontier",
        },
    )


def test_an_approval_that_named_no_tier_still_matches_a_call_without_one() -> None:
    """A tier argument only when set, so approvals predating tiers still work."""
    stored = _confirmation_tool_arguments(
        target_service_id="target_profile",
        user_request="do the thing",
        confirm_delegation=True,
        attachment_ids=None,
        resume_delegation_id=None,
        model_tier=None,
    )

    assert "model_tier" not in stored
    assert _durable_authorization_matches(
        _stored_approval(**stored),
        {
            "target_service_id": "target_profile",
            "user_request": "do the thing",
            "confirm_delegation": True,
            "model_tier": None,
        },
    )


def _delegate_descriptor() -> ToolDescriptor:
    for descriptor in LOCAL_TOOL_DESCRIPTORS:
        if descriptor.name == "delegate_to_service":
            return descriptor
    raise AssertionError("delegate_to_service descriptor not found")


def test_the_synthetic_self_delegation_allow_does_not_bypass_the_tier_gate(
    shipped_config: AppConfig,
) -> None:
    """A profile delegating to itself is still held to its `auto_model_tiers`.

    The policy builder injects a self-delegation ALLOW at the `profile` layer on
    the assumption that self-delegation cannot escalate. A spend-selecting
    argument breaks that assumption, so admission has to be its own gate: the
    policy engine says yes to the call either way, and the gate is what refuses
    `frontier` while accepting `deep`.
    """
    profile = shipped_profile(shipped_config, "default_assistant")
    eligibility = ModelTierEligibility.from_profile(profile, shipped_config.model_tiers)
    engine = _build_profile_policy_engine(
        profile.id,
        profile.tools_policy,
        profile.operator_tools_policy,
        shipped_config.global_tools_policy,
        profile.excluded_global_tools,
    )

    to_itself = engine.evaluate_for_execution(
        _delegate_descriptor(),
        arguments={"target_service_id": profile.id, "model_tier": "frontier"},
        can_confirm=True,
    )
    assert to_itself.decision is ToolPolicyDecision.ALLOW

    with pytest.raises(ModelTierNotPermitted):
        resolve_model_selection(
            eligibility,
            ModelSelectionRequest(tier="frontier", source="model"),
            profile_id=profile.id,
        )
    admitted = resolve_model_selection(
        eligibility,
        ModelSelectionRequest(tier="deep", source="model"),
        profile_id=profile.id,
    )
    assert admitted.tier == "deep"
