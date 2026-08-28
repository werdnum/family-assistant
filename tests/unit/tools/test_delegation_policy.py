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
from family_assistant.config_models import ToolsConfig
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
from family_assistant.tools.confirmation import MAX_DELEGATION_REQUEST_CHARS
from family_assistant.tools.services import delegate_to_service_tool
from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.a2a.client import A2AClientWrapper
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.processing import ProcessingService


class _Namespace:
    """Small attribute bag for test service doubles."""

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
    ) -> Task:
        _ = content_parts
        _ = task_id
        _ = metadata
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
        db_context=MagicMock(spec=Database),
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
        db_context=MagicMock(spec=Database),
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
        db_context=MagicMock(spec=Database),
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
        db_context=MagicMock(spec=Database),
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
