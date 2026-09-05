"""Fail-fast and parity behavior tests for the processing module."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import yaml

from family_assistant.config_loader import load_config
from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import (
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    ErrorMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.model_selection import (
    ModelTierEligibility,
    ModelTierOption,
    ResolvedModelSelection,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.attachments import (
    AttachmentProcessor,
    AttachmentSelectionError,
)
from family_assistant.processing.context import ContextPreparer
from family_assistant.processing.types import ToolExecutionResult
from family_assistant.storage.database import Database
from family_assistant.tools.types import (
    ToolAttachment,
    ToolCallReviewTurnState,
    ToolResult,
)
from family_assistant.utils.clock import SystemClock
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import ContentPartDict
    from family_assistant.tools import ToolExecutionContext
    from family_assistant.tools.types import ToolDefinition


class SimpleToolsProvider:
    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return []

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool args are dynamic
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return ""

    async def close(self) -> None:
        pass


def _make_service(
    llm_client: RuleBasedMockLLMClient | None = None,
    max_iterations: int = 5,
) -> ProcessingService:
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helper."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="processing_fail_fast_test",
        max_iterations=max_iterations,
    )
    return ProcessingService(
        llm_client=llm_client
        or RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="ok"),
        ),
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )


@pytest.mark.asyncio
async def test_sync_forwards_chat_interfaces_to_process_message(
    db_engine: AsyncEngine,
) -> None:
    service = _make_service()
    process_message_mock = AsyncMock(return_value=([], None, None))
    service.process_message = process_message_mock  # type: ignore[method-assign]

    db_context = Database(db_engine)
    await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="test",
        conversation_id="conv_sync_forwarding",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-1",
        user_name="tester",
        chat_interfaces={"web": MagicMock()},
    )

    assert process_message_mock.await_args is not None
    captured_kwargs = process_message_mock.await_args.kwargs
    assert "chat_interfaces" in captured_kwargs
    assert isinstance(captured_kwargs["chat_interfaces"], dict)
    assert "web" in captured_kwargs["chat_interfaces"]


@pytest.mark.asyncio
async def test_stream_forwards_user_and_subconversation_to_process_message_stream(
    db_engine: AsyncEngine,
) -> None:
    service = _make_service()
    captured_kwargs: dict[str, object] = {}

    async def fake_process_message_stream(
        **kwargs: object,
    ) -> AsyncIterator[tuple[LLMStreamEvent, AssistantMessage | None]]:
        captured_kwargs.update(kwargs)
        yield (
            LLMStreamEvent(type="done", metadata={}),
            AssistantMessage(content="stream response"),
        )

    service.process_message_stream = fake_process_message_stream  # type: ignore[method-assign]

    db_context = Database(db_engine)
    events: list[LLMStreamEvent] = []
    async for event in service.handle_chat_interaction_stream(
        db_context=db_context,
        interface_type="test",
        conversation_id="conv_stream_forwarding",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-2",
        user_name="tester",
        user_id="user-123",
        subconversation_id="sub-abc",
    ):
        events.append(event)

    assert events
    assert captured_kwargs.get("user_id") == "user-123"
    assert captured_kwargs.get("subconversation_id") == "sub-abc"


@pytest.mark.no_db
@pytest.mark.no_db
@pytest.mark.asyncio
async def test_sync_and_stream_share_error_persistence_helper() -> None:
    service = _make_service()
    db_context = MagicMock()
    service._prepare_turn_messages_for_llm = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, [])
    )
    service.process_message = AsyncMock(side_effect=RuntimeError("sync boom"))  # type: ignore[method-assign]

    async def fake_process_message_stream(
        **kwargs: object,
    ) -> AsyncIterator[tuple[LLMStreamEvent, AssistantMessage | None]]:
        if False:  # pragma: no cover - keeps this as an async generator
            yield (LLMStreamEvent(type="done"), None)
        raise RuntimeError("stream boom")

    service.process_message_stream = fake_process_message_stream  # type: ignore[method-assign]
    persist_error_mock = AsyncMock(return_value=99)
    service._persist_error_history_message = persist_error_mock  # type: ignore[method-assign]

    sync_result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="test",
        conversation_id="conv_sync_shared_error",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-sync-shared-error",
        user_name="tester",
    )
    stream_events = [
        event
        async for event in service.handle_chat_interaction_stream(
            db_context=db_context,
            interface_type="test",
            conversation_id="conv_stream_shared_error",
            trigger_content_parts=[{"type": "text", "text": "hello"}],
            trigger_interface_message_id="msg-stream-shared-error",
            user_name="tester",
        )
    ]

    assert sync_result.has_error
    assert any(event.type == "error" for event in stream_events)
    assert persist_error_mock.await_count == 2
    for await_call in persist_error_mock.await_args_list:
        assert await_call.kwargs["interface_type"] == "test"
        assert await_call.kwargs["error_traceback"]


@pytest.mark.asyncio
async def test_sync_persists_errors_as_error_messages(db_engine: AsyncEngine) -> None:
    service = _make_service()
    service.process_message = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    db_context = Database(db_engine)
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="test",
        conversation_id="conv_sync_error_message",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-3",
        user_name="tester",
    )

    assert result.has_error

    saved_messages = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id="conv_sync_error_message",
        limit=10,
        max_age=timedelta(hours=24),
        processing_profile_id=service.service_config.id,
        subconversation_id=None,
        current_time=service.clock.now(),
    )
    assert any(isinstance(message, ErrorMessage) for message in saved_messages)


@pytest.mark.asyncio
async def test_stream_persists_errors_as_error_messages(db_engine: AsyncEngine) -> None:
    service = _make_service()

    async def fake_process_message_stream(
        **kwargs: object,
    ) -> AsyncIterator[tuple[LLMStreamEvent, AssistantMessage | None]]:
        if False:  # pragma: no cover - keeps this as an async generator
            yield (LLMStreamEvent(type="done"), None)
        raise RuntimeError("stream boom")

    service.process_message_stream = fake_process_message_stream  # type: ignore[method-assign]

    db_context = Database(db_engine)
    events: list[LLMStreamEvent] = []
    async for event in service.handle_chat_interaction_stream(
        db_context=db_context,
        interface_type="test",
        conversation_id="conv_stream_error_message",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-4",
        user_name="tester",
    ):
        events.append(event)

    assert any(event.type == "error" for event in events)

    saved_messages = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id="conv_stream_error_message",
        limit=10,
        max_age=timedelta(hours=24),
        processing_profile_id=service.service_config.id,
        subconversation_id=None,
        current_time=service.clock.now(),
    )
    assert any(isinstance(message, ErrorMessage) for message in saved_messages)


async def test_steer_echo_is_published_only_after_it_is_persisted(
    db_engine: AsyncEngine,
) -> None:
    """A user_input echo must not reach the client before its row is written.

    The web client treats the echo as proof its steering message was delivered
    and stops tracking it for recovery. Publishing before the save would let a
    failed write leave the message existing nowhere -- neither persisted nor
    acted on, and no longer recoverable by the sender.
    """
    service = _make_service()
    steer_text = "actually, focus on tomorrow"

    async def fake_process_message_stream(
        **kwargs: object,
    ) -> AsyncIterator[tuple[LLMStreamEvent, UserMessage | AssistantMessage | None]]:
        yield (
            LLMStreamEvent(type="user_input", content=steer_text, input_id="input-1"),
            UserMessage(content=steer_text),
        )
        yield (
            LLMStreamEvent(type="content", content="done"),
            AssistantMessage(content="done"),
        )

    service.process_message_stream = fake_process_message_stream  # type: ignore[method-assign]

    original_save = service._save_history_message

    async def failing_save(*args: object, **kwargs: object) -> object:
        message = kwargs.get("message")
        if isinstance(message, UserMessage) and message.content == steer_text:
            raise RuntimeError("history write failed")
        return await original_save(*args, **kwargs)  # type: ignore[arg-type]

    service._save_history_message = failing_save  # type: ignore[method-assign]

    events: list[LLMStreamEvent] = []
    async for event in service.handle_chat_interaction_stream(
        db_context=Database(db_engine),
        interface_type="test",
        conversation_id="conv_steer_echo_order",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-echo",
        user_name="tester",
    ):
        events.append(event)

    assert not any(event.type == "user_input" for event in events), (
        "The echo was published even though its row was never written, so the "
        "client would treat an unpersisted message as delivered"
    )
    assert any(event.type == "error" for event in events)


@pytest.mark.no_db
def test_llm_loop_infers_attachment_types_from_mime_type() -> None:
    service = _make_service()

    assert service.llm_loop._infer_attachment_type("image/png") == "image"
    assert service.llm_loop._infer_attachment_type("video/mp4") == "video"
    assert service.llm_loop._infer_attachment_type("audio/mpeg") == "audio"
    assert service.llm_loop._infer_attachment_type("application/pdf") == "document"
    assert service.llm_loop._infer_attachment_type("text/plain") == "file"
    assert service.llm_loop._infer_attachment_type(None) == "file"


@pytest.mark.no_db
def test_tool_execution_result_applies_attachment_updates_consistently() -> None:
    result = ToolExecutionResult(
        stream_event=LLMStreamEvent(type="tool_result", tool_call_id="call-1"),
        llm_message=ToolMessage(
            tool_call_id="call-1",
            content="ok",
            name="example_tool",
        ),
        auto_attachment_ids=["auto-1", "auto-2"],
        explicit_attachment_ids=["explicit-1"],
    )
    pending_attachment_ids = ["existing", "auto-1"]

    result.apply_attachment_updates(pending_attachment_ids)

    assert pending_attachment_ids == ["explicit-1"]


@pytest.mark.asyncio
async def test_final_iteration_tool_calls_do_not_raise_processing_error(
    db_engine: AsyncEngine,
) -> None:
    tool_call = ToolCallItem(
        id="call_final_iteration",
        type="function",
        function=ToolCallFunction(name="example_tool", arguments='{"x": 1}'),
    )
    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content=None, tool_calls=[tool_call]),
    )
    service = _make_service(llm_client=llm_client, max_iterations=1)

    db_context = Database(db_engine)
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="test",
        conversation_id="conv_final_iteration_tool_calls",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-final-1",
        user_name="tester",
    )

    assert not result.has_error


@pytest.mark.asyncio
async def test_sync_fails_fast_when_tool_executor_raises_unexpected_exception(
    db_engine: AsyncEngine,
) -> None:
    tool_call = ToolCallItem(
        id="call_executor_crash_sync",
        type="function",
        function=ToolCallFunction(name="example_tool", arguments='{"x": 1}'),
    )
    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content=None, tool_calls=[tool_call]),
    )
    service = _make_service(llm_client=llm_client, max_iterations=3)
    service.tool_executor.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("unexpected tool executor crash")
    )

    db_context = Database(db_engine)
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="test",
        conversation_id="conv_executor_crash_sync",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-executor-crash-sync",
        user_name="tester",
    )

    assert result.has_error
    assert result.error_traceback is not None
    assert "unexpected tool executor crash" in result.error_traceback

    saved_messages = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id="conv_executor_crash_sync",
        limit=10,
        max_age=timedelta(hours=24),
        processing_profile_id=service.service_config.id,
        subconversation_id=None,
        current_time=service.clock.now(),
    )
    assert any(isinstance(message, ErrorMessage) for message in saved_messages)


@pytest.mark.asyncio
async def test_stream_emits_error_when_tool_executor_raises_unexpected_exception(
    db_engine: AsyncEngine,
) -> None:
    tool_call = ToolCallItem(
        id="call_executor_crash_stream",
        type="function",
        function=ToolCallFunction(name="example_tool", arguments='{"x": 1}'),
    )
    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content=None, tool_calls=[tool_call]),
    )
    service = _make_service(llm_client=llm_client, max_iterations=3)
    service.tool_executor.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("unexpected tool executor crash")
    )

    db_context = Database(db_engine)
    events: list[LLMStreamEvent] = []
    async for event in service.handle_chat_interaction_stream(
        db_context=db_context,
        interface_type="test",
        conversation_id="conv_executor_crash_stream",
        trigger_content_parts=[{"type": "text", "text": "hello"}],
        trigger_interface_message_id="msg-executor-crash-stream",
        user_name="tester",
    ):
        events.append(event)

    assert any(event.type == "error" for event in events)

    saved_messages = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id="conv_executor_crash_stream",
        limit=10,
        max_age=timedelta(hours=24),
        processing_profile_id=service.service_config.id,
        subconversation_id=None,
        current_time=service.clock.now(),
    )
    assert any(isinstance(message, ErrorMessage) for message in saved_messages)


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_context_aggregate_context_raises_on_provider_failure() -> None:
    class BrokenProvider:
        name = "broken-provider"

        async def get_context_fragments(self) -> list[str]:
            raise RuntimeError("provider failed")

    config = ProcessingServiceConfig(
        prompts={},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="context-fail-fast",
    )
    preparer = ContextPreparer(
        context_providers=[BrokenProvider()],
        config=config,
        clock=SystemClock(),
    )

    with pytest.raises(
        RuntimeError,
        match="Context provider 'broken-provider' failed to provide fragments",
    ):
        await preparer.aggregate_context()


@pytest.mark.no_db
def test_render_system_prompt_raises_on_unknown_placeholder() -> None:
    service = _make_service()
    service.service_config.prompts["system_prompt"] = (
        "Link: {server_url}/automations/{automation_id}"
    )

    with pytest.raises(
        ValueError,
        match="System prompt template contains unknown placeholders: automation_id",
    ):
        service.format_system_prompt(user_name="tester")


@pytest.mark.no_db
def test_render_system_prompt_allows_escaped_literal_braces() -> None:
    service = _make_service()
    service.service_config.prompts["system_prompt"] = (
        "Link: {server_url}/automations/{{automation_id}}"
    )

    rendered_prompt = service.format_system_prompt(user_name="tester")

    assert "http://testserver/automations/{automation_id}" in rendered_prompt


@pytest.mark.no_db
def test_render_system_prompt_supports_placeholder_adjacent_to_escaped_braces() -> None:
    service = _make_service()
    service.service_config.prompts["system_prompt"] = "Wrapped: {{{server_url}}}"

    rendered_prompt = service.format_system_prompt(user_name="tester")

    assert "{http://testserver}" in rendered_prompt


@pytest.mark.no_db
def test_all_processing_profile_system_prompts_can_be_rendered() -> None:
    """What startup validation checks, over every shipped profile."""
    app_config = load_config(load_dotenv_file=False)

    profile_processing_configs = [
        (
            "default_profile_settings",
            app_config.default_profile_settings.processing_config,
        ),
        *(
            (profile.id, profile.processing_config)
            for profile in app_config.service_profiles
        ),
    ]

    for profile_id, processing_config in profile_processing_configs:
        service = _make_service()
        service.service_config.id = profile_id
        service.service_config.prompts = processing_config.prompts

        service.validate_system_prompt_renders()

        assert service.format_system_prompt(user_name="tester"), profile_id


@pytest.mark.no_db
def test_default_system_prompt_templates_only_use_supported_placeholders() -> None:
    defaults_path = Path(__file__).resolve().parents[3] / "defaults.yaml"
    with defaults_path.open(encoding="utf-8") as defaults_file:
        config = yaml.safe_load(defaults_file)

    # current_time and aggregated_other_context are deliberately absent: they
    # moved into the trailing turn-context block, and a template that reaches for
    # either would put volatile text back ahead of the conversation history and
    # silently cost the prompt cache. See docs/design/prompt-cache-turn-context.md.
    allowed_placeholders = {
        "profile_id",
        "server_url",
        "user_name",
    }
    invalid_placeholders_by_profile: dict[str, list[str]] = {}
    placeholder_pattern = r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})"

    profile_configs = [
        ("default_profile_settings", config["default_profile_settings"]),
        *((profile["id"], profile) for profile in config.get("service_profiles", [])),
    ]
    for profile_id, profile_config in profile_configs:
        processing_config = profile_config.get("processing_config") or {}
        prompts = processing_config.get("prompts") or {}
        system_prompt = prompts.get("system_prompt")
        if not isinstance(system_prompt, str):
            continue

        placeholders = sorted(
            set(re.findall(placeholder_pattern, system_prompt)) - allowed_placeholders
        )
        if placeholders:
            invalid_placeholders_by_profile[profile_id] = placeholders

    assert invalid_placeholders_by_profile == {}


class _DelegateAdvertisingToolsProvider(SimpleToolsProvider):
    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "delegate_to_service",
                    "description": "Delegate to another profile.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]


def _registry_with_profiles() -> Any:  # noqa: ANN401 - test stub registry
    return cast(
        "Any",
        {
            "browser_profile": SimpleNamespace(
                service_config=SimpleNamespace(
                    id="browser_profile",
                    description="Drives a web browser.",
                    tier_eligibility=ModelTierEligibility(),
                )
            ),
            "tiered_profile": SimpleNamespace(
                service_config=SimpleNamespace(
                    id="tiered_profile",
                    description="Thinks hard.",
                    tier_eligibility=ModelTierEligibility(
                        default_tier="standard",
                        selectable=(
                            ModelTierOption(id="standard", label="Standard"),
                            ModelTierOption(
                                id="deep", label="Deep", description="Hard problems."
                            ),
                        ),
                        auto=frozenset({"standard", "deep"}),
                    ),
                )
            ),
            "bare_profile": SimpleNamespace(
                service_config=SimpleNamespace(
                    id="bare_profile",
                    description="",
                    tier_eligibility=ModelTierEligibility(),
                )
            ),
        },
    )


@pytest.mark.no_db
def test_render_available_service_profiles_without_registry() -> None:
    service = _make_service()

    assert not service.render_available_service_profiles()


@pytest.mark.no_db
def test_render_available_service_profiles_lists_registry_profiles() -> None:
    service = _make_service()
    service.processing_services_registry = _registry_with_profiles()

    rendered = service.render_available_service_profiles()

    assert "- ID: browser_profile, Description: Drives a web browser." in rendered
    assert "- ID: bare_profile, Description: No description available." in rendered


@pytest.mark.no_db
def test_the_catalog_advertises_only_tiers_a_delegation_may_actually_ask_for() -> None:
    """A tier the target would refuse from another profile is not a suggestion."""
    service = _make_service()
    service.processing_services_registry = _registry_with_profiles()

    rendered = service.render_available_service_profiles()

    assert "- deep (Deep): Hard problems." in rendered
    # The target's own default needs no naming, and a pinned profile has none.
    assert "standard" not in rendered
    assert "model_tier" not in rendered.split("tiered_profile")[0]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_delegation_catalog_addition_empty_when_cannot_delegate() -> None:
    service = _make_service()
    service.processing_services_registry = _registry_with_profiles()

    assert not await service.delegation_catalog_addition()


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_delegation_catalog_addition_lists_profiles_when_advertised() -> None:
    service = _make_service()
    service.tools_provider = _DelegateAdvertisingToolsProvider()
    service.processing_services_registry = _registry_with_profiles()

    addition = await service.delegation_catalog_addition()

    assert "delegate_to_service" in addition
    assert "- ID: browser_profile, Description: Drives a web browser." in addition


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_process_content_parts_missing_attachment_id_raises() -> None:
    processor = AttachmentProcessor(
        attachment_registry=AsyncMock(),
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(ValueError, match="missing required 'attachment_id'"):
        await processor.process_content_parts(
            db_context=MagicMock(),
            conversation_id="conv",
            content_parts=cast("list[ContentPartDict]", [{"type": "attachment"}]),
            acting_user_id=None,
            llm_client=MagicMock(),
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_process_content_parts_missing_attachment_metadata_raises() -> None:
    mock_registry = AsyncMock()
    mock_registry.get_attachment.return_value = None
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(ValueError, match="was not found in attachment registry"):
        await processor.process_content_parts(
            db_context=MagicMock(),
            conversation_id="conv",
            content_parts=cast(
                "list[ContentPartDict]",
                [{"type": "attachment", "attachment_id": "att-1"}],
            ),
            acting_user_id=None,
            llm_client=MagicMock(),
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_process_content_parts_missing_attachment_content_raises() -> None:
    mock_registry = AsyncMock()
    mock_registry.get_attachment.return_value = MagicMock(mime_type="text/plain")
    mock_registry.get_attachment_content.return_value = None
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(RuntimeError, match="content could not be retrieved"):
        await processor.process_content_parts(
            db_context=MagicMock(),
            conversation_id="conv",
            content_parts=cast(
                "list[ContentPartDict]",
                [{"type": "attachment", "attachment_id": "att-1"}],
            ),
            acting_user_id=None,
            llm_client=MagicMock(),
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_convert_urls_to_data_uris_invalid_internal_url_raises() -> None:
    mock_registry = MagicMock()
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(ValueError, match="Invalid attachment URL format"):
        await processor.convert_urls_to_data_uris(
            MagicMock(),
            [
                {
                    "type": "image_url",
                    "image_url": {"url": "/api/attachments/not-a-uuid"},
                }
            ],
            acting_user_id=None,
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_convert_urls_to_data_uris_missing_file_raises() -> None:
    mock_registry = MagicMock()
    mock_registry.resolve_attachment_path = AsyncMock(
        return_value=Path("/tmp/file-does-not-exist.png")
    )
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(FileNotFoundError, match="Attachment file not found"):
        await processor.convert_urls_to_data_uris(
            MagicMock(),
            [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "/api/attachments/550e8400-e29b-41d4-a716-446655440000"
                    },
                }
            ],
            acting_user_id=None,
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_extract_conversation_context_propagates_registry_failures() -> None:
    mock_registry = AsyncMock()
    mock_registry.get_recent_attachments_for_conversation.side_effect = RuntimeError(
        "attachment context query failed"
    )
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(RuntimeError, match="attachment context query failed"):
        await processor.extract_conversation_context(
            db_context=MagicMock(),
            conversation_id="conv",
            max_age_hours=24,
            prompts={},
            acting_user_id=None,
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_stream_done_attachment_metadata_lookup_propagates_failures() -> None:
    class TwoStepToolThenContentLLM:
        def __init__(self) -> None:
            self.call_count = 0

        async def generate_response_stream(
            self,
            *,
            messages: list[object],
            tools: list[dict[str, object]] | None,
            tool_choice: str,
        ) -> AsyncIterator[LLMStreamEvent]:
            self.call_count += 1
            if self.call_count == 1:
                yield LLMStreamEvent(
                    type="tool_call",
                    tool_call=ToolCallItem(
                        id="call-1",
                        type="function",
                        function=ToolCallFunction(name="example_tool", arguments="{}"),
                    ),
                )
                yield LLMStreamEvent(type="done", metadata={})
                return

            yield LLMStreamEvent(type="content", content="final answer")
            yield LLMStreamEvent(type="done", metadata={})

    service = _make_service(llm_client=cast("Any", TwoStepToolThenContentLLM()))
    service.tool_executor.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=ToolExecutionResult(
            stream_event=LLMStreamEvent(
                type="tool_result",
                tool_call_id="call-1",
                tool_result="ok",
            ),
            llm_message=ToolMessage(
                tool_call_id="call-1",
                content="ok",
                name="example_tool",
            ),
            auto_attachment_ids=["missing-att"],
            explicit_attachment_ids=None,
        )
    )
    mock_registry = AsyncMock()
    mock_registry.get_attachment.return_value = None
    service.attachment_processor.attachment_registry = mock_registry

    with pytest.raises(
        ValueError, match="Missing metadata for pending attachment 'missing-att'"
    ):
        async for _event, _message in service.llm_loop.run_stream(
            db_context=MagicMock(),
            messages=[
                SystemMessage(content="system"),
                UserMessage(content="hello"),
            ],
            interface_type="test",
            conversation_id="conv",
            user_name="tester",
            turn_id="turn",
            chat_interface=None,
            llm_client=service.llm_client,
            model_selection=ResolvedModelSelection.unselected(None),
            processing_service=service,
        ):
            pass


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_denial_escalation_ends_after_complete_tool_result_batch_without_model() -> (
    None
):
    class ModelCallMustNotRepeat:
        def __init__(self) -> None:
            self.call_count = 0

        async def generate_response_stream(
            self,
            *,
            messages: list[object],
            tools: list[dict[str, object]] | None,
            tool_choice: str,
        ) -> AsyncIterator[LLMStreamEvent]:
            del messages, tools, tool_choice
            self.call_count += 1
            if self.call_count > 1:
                raise AssertionError("termination must prevent another model call")
            for call_id in ("denied-call", "batch-peer-call"):
                yield LLMStreamEvent(
                    type="tool_call",
                    tool_call=ToolCallItem(
                        id=call_id,
                        type="function",
                        function=ToolCallFunction(name="example_tool", arguments="{}"),
                    ),
                )
            yield LLMStreamEvent(type="done", metadata={})

    llm_client = ModelCallMustNotRepeat()
    service = _make_service(llm_client=cast("Any", llm_client))

    async def execute_with_terminal_escalation(
        tool_call: ToolCallItem,
        **kwargs: object,
    ) -> ToolExecutionResult:
        review_state = cast("ToolCallReviewTurnState", kwargs["tool_call_review_state"])
        if tool_call.id == "denied-call":
            review_state.terminal_denial_escalation_message = (
                "Automatic review stopped this turn after repeated denials."
            )
            # Let the peer finish first to prove termination waits for the full
            # parallel batch rather than returning with an unanswered tool call.
            # ast-grep-ignore: no-asyncio-sleep-in-tests - Yield to the peer task.
            await asyncio.sleep(0)
        return ToolExecutionResult(
            stream_event=LLMStreamEvent(
                type="tool_result",
                tool_call_id=tool_call.id,
                tool_result=f"result for {tool_call.id}",
            ),
            llm_message=ToolMessage(
                tool_call_id=tool_call.id,
                content=f"result for {tool_call.id}",
                name="example_tool",
            ),
        )

    service.tool_executor.execute = execute_with_terminal_escalation  # type: ignore[method-assign]
    emitted: list[tuple[LLMStreamEvent, object | None]] = []
    async for event, message in service.llm_loop.run_stream(
        db_context=MagicMock(),
        messages=[
            SystemMessage(content="system"),
            UserMessage(content="hello"),
        ],
        interface_type="test",
        conversation_id="conv",
        user_name="tester",
        turn_id="turn",
        chat_interface=None,
        llm_client=service.llm_client,
        model_selection=ResolvedModelSelection.unselected(None),
        processing_service=service,
    ):
        emitted.append((event, message))

    tool_results = [
        (index, event.tool_call_id)
        for index, (event, _message) in enumerate(emitted)
        if event.type == "tool_result"
    ]
    terminal_content_index = next(
        index
        for index, (event, _message) in enumerate(emitted)
        if event.type == "content"
        and event.content
        == "Automatic review stopped this turn after repeated denials."
    )
    assert {call_id for _index, call_id in tool_results} == {
        "denied-call",
        "batch-peer-call",
    }
    assert all(index < terminal_content_index for index, _call_id in tool_results)
    assert llm_client.call_count == 1
    final_message = emitted[-1][1]
    assert isinstance(final_message, AssistantMessage)
    assert (
        final_message.content
        == "Automatic review stopped this turn after repeated denials."
    )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_stream_attachment_selection_failure_uses_capped_auto_queue() -> None:
    class TwoStepToolThenContentLLM:
        def __init__(self) -> None:
            self.call_count = 0

        async def generate_response_stream(
            self,
            *,
            messages: list[object],
            tools: list[dict[str, object]] | None,
            tool_choice: str,
        ) -> AsyncIterator[LLMStreamEvent]:
            self.call_count += 1
            if self.call_count == 1:
                yield LLMStreamEvent(
                    type="tool_call",
                    tool_call=ToolCallItem(
                        id="call-1",
                        type="function",
                        function=ToolCallFunction(name="example_tool", arguments="{}"),
                    ),
                )
                yield LLMStreamEvent(
                    type="tool_call",
                    tool_call=ToolCallItem(
                        id="call-2",
                        type="function",
                        function=ToolCallFunction(name="example_tool", arguments="{}"),
                    ),
                )
                yield LLMStreamEvent(type="done", metadata={})
                return

            yield LLMStreamEvent(type="content", content="final answer")
            yield LLMStreamEvent(type="done", metadata={})

    service = _make_service(llm_client=cast("Any", TwoStepToolThenContentLLM()))
    service.app_config.attachment_selection_threshold = 0
    service.app_config.max_response_attachments = 1
    service.tool_executor.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id="call-1",
                    tool_result="ok",
                ),
                llm_message=ToolMessage(
                    tool_call_id="call-1",
                    content="ok",
                    name="example_tool",
                ),
                auto_attachment_ids=["att-1"],
                explicit_attachment_ids=None,
            ),
            ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id="call-2",
                    tool_result="ok",
                ),
                llm_message=ToolMessage(
                    tool_call_id="call-2",
                    content="ok",
                    name="example_tool",
                ),
                auto_attachment_ids=["att-2"],
                explicit_attachment_ids=None,
            ),
        ],
    )
    service.attachment_processor.select_for_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=AttachmentSelectionError("selection failed")
    )

    done_event = None
    async for event, _message in service.llm_loop.run_stream(
        db_context=MagicMock(),
        messages=[
            SystemMessage(content="system"),
            UserMessage(content="hello"),
        ],
        interface_type="test",
        conversation_id="conv",
        user_name="tester",
        turn_id="turn",
        chat_interface=None,
        llm_client=service.llm_client,
        model_selection=ResolvedModelSelection.unselected(None),
        processing_service=service,
    ):
        if event.type == "done":
            done_event = event

    assert done_event is not None
    assert done_event.metadata is not None
    attachment_ids = done_event.metadata.get("attachment_ids")
    assert attachment_ids == ["att-1"]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_tool_executor_propagates_attachment_storage_failures() -> None:
    service = _make_service()
    mock_tools_provider = AsyncMock()
    mock_tools_provider.execute_tool.return_value = ToolResult(
        text="ok",
        attachments=[
            ToolAttachment(
                mime_type="image/png",
                content=b"test-bytes",
                description="test attachment",
            )
        ],
    )
    service.tool_executor.tools_provider = mock_tools_provider

    mock_registry = AsyncMock()
    mock_registry.store_and_register_tool_attachment.side_effect = RuntimeError(
        "attachment storage failed"
    )
    service.tool_executor.attachment_registry = mock_registry

    tool_call = ToolCallItem(
        id="call_attachment_store_fail",
        type="function",
        function=ToolCallFunction(name="tool_with_attachment", arguments="{}"),
    )

    with pytest.raises(RuntimeError, match="attachment storage failed"):
        await service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv",
            user_name="tester",
            turn_id="turn",
            db_context=MagicMock(),
            chat_interface=None,
            request_confirmation_callback=None,
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_tool_executor_propagates_attach_to_response_metadata_failures() -> None:
    service = _make_service()
    mock_tools_provider = AsyncMock()
    mock_tools_provider.execute_tool.return_value = (
        '{"status":"attachments_queued","attachment_ids":["missing-attachment"]}'
    )
    service.tool_executor.tools_provider = mock_tools_provider

    mock_registry = AsyncMock()
    mock_registry.get_attachment.return_value = None
    service.tool_executor.attachment_registry = mock_registry

    tool_call = ToolCallItem(
        id="call_attach_metadata_fail",
        type="function",
        function=ToolCallFunction(name="attach_to_response", arguments="{}"),
    )

    with pytest.raises(
        ValueError,
        match="attach_to_response referenced unknown attachment",
    ):
        await service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv",
            user_name="tester",
            turn_id="turn",
            db_context=MagicMock(),
            chat_interface=None,
            request_confirmation_callback=None,
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_tool_executor_rejects_missing_tool_call_id() -> None:
    service = _make_service()
    tool_call = ToolCallItem(
        id="",
        type="function",
        function=ToolCallFunction(name="example_tool", arguments="{}"),
    )

    with pytest.raises(ValueError, match="Tool call must include a non-empty id"):
        await service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv",
            user_name="tester",
            turn_id="turn",
            db_context=MagicMock(),
            chat_interface=None,
            request_confirmation_callback=None,
        )


def test_large_result_attachments_are_not_queued_for_display() -> None:
    """Auto-converted oversized results stay out of the response attachments."""
    result = ToolExecutionResult(
        stream_event=LLMStreamEvent(type="tool_result", tool_call_id="call-1"),
        llm_message=ToolMessage(
            tool_call_id="call-1",
            content="ok",
            name="example_tool",
        ),
        auto_attachment_ids=["chart-1"],
        large_result_attachment_ids=["large-1"],
    )
    pending_attachment_ids: list[str] = []

    result.apply_attachment_updates(pending_attachment_ids)

    assert pending_attachment_ids == ["chart-1"]
