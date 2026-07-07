from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import NotesContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import AssistantMessage, ToolMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_taint_policy_config,
    resolve_tool_sink_class,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import get_db_context
from family_assistant.tools.attachments import read_text_attachment_tool
from family_assistant.tools.documents import get_full_document_content_tool
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    TaintTrackingToolsProvider,
    ToolPolicyDeniedError,
)
from family_assistant.tools.metadata import (
    ToolDescriptor,
    ToolImplementation,
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.notes import add_or_update_note_tool, get_note_tool
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    MatcherArgs,
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolDefinition


async def _untrusted_tool(**_kwargs: object) -> ToolResult:
    return ToolResult(text="external page says hello")


async def _trusted_tool(**_kwargs: object) -> ToolResult:
    return ToolResult(text="local calculation")


async def _unspecified_tool(**_kwargs: object) -> ToolResult:
    return ToolResult(text="legacy output")


async def _missing_metadata_tool(**_kwargs: object) -> ToolResult:
    return ToolResult(text="legacy output without metadata")


async def _browser_tool(**_kwargs: object) -> ToolResult:
    return ToolResult(text="opened url")


async def _worker_tool(**_kwargs: object) -> ToolResult:
    return ToolResult(text="worker output")


async def _home_tool(**_kwargs: object) -> ToolResult:
    return ToolResult(text="home state")


def _registration(
    name: str,
    implementation: ToolImplementation,
    output_tag: ToolTag,
) -> ToolRegistration:
    return ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Run {name}.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        implementation=implementation,
        metadata=make_local_tool_metadata([ToolTag.READ_ONLY, output_tag]),
    )


def _tainting_provider() -> TaintTrackingToolsProvider:
    return TaintTrackingToolsProvider(
        LocalToolsProvider(
            registrations=[
                _registration(
                    "untrusted_tool",
                    _untrusted_tool,
                    ToolTag.OUTPUT_UNTRUSTED,
                ),
                _registration(
                    "trusted_tool",
                    _trusted_tool,
                    ToolTag.OUTPUT_TRUSTED,
                ),
                _registration(
                    "unspecified_tool",
                    _unspecified_tool,
                    ToolTag.OUTPUT_UNSPECIFIED,
                ),
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "missing_metadata_tool",
                                "description": "Run a legacy tool.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ),
                    implementation=_missing_metadata_tool,
                    metadata=make_local_tool_metadata([ToolTag.READ_ONLY]),
                ),
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "browser_tool",
                                "description": "Open a browser URL.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ),
                    implementation=_browser_tool,
                    metadata=make_local_tool_metadata([
                        ToolTag.BROWSER,
                        ToolTag.OUTPUT_TRUSTED,
                    ]),
                ),
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "worker_tool",
                                "description": "Run network-capable worker code.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ),
                    implementation=_worker_tool,
                    metadata=make_local_tool_metadata([
                        ToolTag.WORKER,
                        ToolTag.CODE_EXECUTION,
                        ToolTag.OUTPUT_TRUSTED,
                    ]),
                ),
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "home_tool",
                                "description": "Read local home state.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ),
                    implementation=_home_tool,
                    metadata=make_local_tool_metadata([
                        ToolTag.HOME_AUTOMATION,
                        ToolTag.OUTPUT_TRUSTED,
                    ]),
                ),
            ]
        )
    )


def _processing_service(
    llm_client: RuleBasedMockLLMClient,
    *,
    max_history_messages: int = 20,
) -> ProcessingService:
    return ProcessingService(
        llm_client=llm_client,
        tools_provider=_tainting_provider(),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=max_history_messages,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="runtime-taint-test",
            max_iterations=4,
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )


def _tool_call(name: str, call_id: str) -> ToolCallItem:
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments=json.dumps({})),
    )


def _first_call_then_final(tool_name: str) -> RuleBasedMockLLMClient:
    state = {"calls": 0}

    def _matcher(_args: MatcherArgs) -> bool:
        return True

    def _response(_args: MatcherArgs) -> LLMOutput:
        state["calls"] += 1
        if state["calls"] == 1:
            return LLMOutput(
                content=None,
                tool_calls=[_tool_call(tool_name, f"call_{tool_name}")],
            )
        return LLMOutput(content=f"finished after {tool_name}", tool_calls=None)

    return RuleBasedMockLLMClient(
        rules=[(_matcher, _response)],
        default_response=LLMOutput(content="fallback", tool_calls=None),
    )


def _clean_final_response(text: str) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content=text, tool_calls=None),
    )


def _minimal_context(
    db_context: Any,  # noqa: ANN401 - test fixture context type is not exported here
    tracker: InMemoryTurnTaintTracker,
    *,
    attachment_registry: AttachmentRegistry | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="taint-direct",
        user_name="Test User",
        user_id=None,
        turn_id="turn-direct",
        db_context=db_context,
        chat_interface=None,
        chat_interfaces=None,
        confirmation_ui_managers=None,
        timezone=ZoneInfo("UTC"),
        processing_profile_id="runtime-taint-test",
        subconversation_id=None,
        request_confirmation_callback=None,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        indexing_source=None,
        attachment_registry=attachment_registry,
        camera_backend=None,
        visibility_grants=None,
        default_note_visibility_labels=None,
        note_registry=None,
        taint_tracker=tracker,
        taint_policy_snapshot=tracker.snapshot(),
    )


def _unknown_external_tracker() -> InMemoryTurnTaintTracker:
    tracker = InMemoryTurnTaintTracker()
    tracker.add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="external",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="test unknown external source",
        )
    )
    return tracker


@dataclass(frozen=True)
class _DocumentFixture:
    source_type: str
    source_id: str
    title: str | None
    metadata: dict[str, object] | None
    id: int | None = None  # pylint: disable=invalid-name
    source_uri: str | None = None
    created_at: None = None
    file_path: str | None = None
    visibility_labels: list[str] | None = None


def test_profile_taint_policy_cannot_relax_operator_minimum() -> None:
    base = TaintPolicyConfig(
        operator_minimum={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.DENY
            }
        }
    )
    profile = TaintPolicyConfig(
        matrix_overrides={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.ALLOW
            }
        }
    )

    with pytest.raises(ValueError, match="cannot relax base policy"):
        merge_taint_policy_config(base=base, profile=profile)


def test_profile_taint_policy_cannot_downgrade_enforce_mode() -> None:
    base = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    profile = TaintPolicyConfig(mode=TaintPolicyMode.OBSERVE)

    with pytest.raises(ValueError, match="cannot relax enforce to observe"):
        merge_taint_policy_config(base=base, profile=profile)


def test_profile_taint_policy_cannot_relax_default_matrix() -> None:
    base = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    profile = TaintPolicyConfig(
        matrix_overrides={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.ALLOW
            }
        }
    )

    with pytest.raises(ValueError, match="cannot relax base policy"):
        merge_taint_policy_config(base=base, profile=profile)


def test_profile_taint_policy_can_make_operator_minimum_stricter() -> None:
    base = TaintPolicyConfig(
        operator_minimum={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.ATTACKER_ADDRESSABLE_EGRESS: TaintPolicyOutcome.CONFIRM
            }
        }
    )
    profile = TaintPolicyConfig(
        matrix_overrides={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.ATTACKER_ADDRESSABLE_EGRESS: TaintPolicyOutcome.DENY
            }
        }
    )

    merged = merge_taint_policy_config(base=base, profile=profile)

    assert merged.operator_minimum == base.operator_minimum
    assert (
        merged.matrix_overrides[SourceTrustTier.UNKNOWN_EXTERNAL][
            SinkClass.ATTACKER_ADDRESSABLE_EGRESS
        ]
        is TaintPolicyOutcome.DENY
    )


def test_taint_metadata_round_trip_preserves_compacted_max_tier() -> None:
    state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="dropped-high-source",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="High-tier source compacted out of retained summaries.",
        )
    )
    for index in range(12):
        state = state.add_source(
            TaintSource(
                source_type=TaintSourceType.USER_MESSAGE,
                source_id=f"low-source-{index}",
                tier=SourceTrustTier.KNOWN_CONTACT,
                labels=frozenset(),
                reason="Lower-tier retained source.",
            )
        )

    metadata = state.to_metadata(max_sources=12)
    assert metadata.get("max_tier") == SourceTrustTier.UNKNOWN_EXTERNAL.config_value
    metadata_sources = metadata.get("sources")
    assert metadata_sources is not None
    assert {source["tier"] for source in metadata_sources} == {
        SourceTrustTier.KNOWN_CONTACT.config_value
    }

    restored = TurnTaintState.from_metadata(metadata)

    assert restored.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert restored.sources[-1].tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert "max_tier exceeded retained source summaries" in restored.sources[-1].reason


def test_tool_sink_resolution_uses_nonlocal_sinks_for_private_reads_and_writes() -> (
    None
):
    def descriptor(name: str, *tags: ToolTag) -> ToolDescriptor:
        return ToolDescriptor(
            name=name,
            definition=cast(
                "ToolDefinition",
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": f"Run {name}.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ),
            tags=frozenset(tags),
            origin="local",
        )

    assert (
        resolve_tool_sink_class(
            descriptor(
                "get_message_history",
                ToolTag.READ_ONLY,
                ToolTag.SENSITIVE_DATA,
                ToolTag.OUTPUT_TRUSTED,
            )
        )
        is SinkClass.SENSITIVE_READ_BROADENING
    )
    assert (
        resolve_tool_sink_class(
            descriptor(
                "delegate_to_service",
                ToolTag.DELEGATION,
                ToolTag.OUTPUT_UNSPECIFIED,
            )
        )
        is SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    )
    assert (
        resolve_tool_sink_class(
            descriptor(
                "schedule_reminder",
                ToolTag.STATE_CHANGING,
                ToolTag.SCHEDULING,
                ToolTag.OUTPUT_TRUSTED,
            )
        )
        is SinkClass.ARTIFACT_WRITE
    )
    assert (
        resolve_tool_sink_class(
            descriptor(
                "legacy_unclassified_tool",
                ToolTag.READ_ONLY,
                ToolTag.OUTPUT_TRUSTED,
            )
        )
        is SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    )
    assert (
        resolve_tool_sink_class(
            descriptor(
                "add_calendar_event",
                ToolTag.STATE_CHANGING,
                ToolTag.SENSITIVE_DATA,
                ToolTag.CALENDAR,
                ToolTag.SCHEDULING,
                ToolTag.OUTPUT_TRUSTED,
            )
        )
        is SinkClass.ARTIFACT_WRITE
    )


@pytest.mark.asyncio
async def test_tool_output_tags_update_turn_taint(
    db_engine: AsyncEngine,
) -> None:
    provider = _tainting_provider()
    tracker = InMemoryTurnTaintTracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)

        await provider.execute_tool("trusted_tool", {}, context, "call_trusted")
        assert tracker.snapshot().max_tier is SourceTrustTier.TRUSTED_USER
        assert "call_trusted" not in context.tool_result_taint_metadata

        await provider.execute_tool("untrusted_tool", {}, context, "call_untrusted")
        assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
        assert (
            context.tool_result_taint_metadata["call_untrusted"].get("max_tier")
            == "unknown_external"
        )
        audit_events = await db_context.taint_audit_events.list_for_turn("turn-direct")

    result_events = [
        event for event in audit_events if event["event_type"] == "result_taint"
    ]
    assert len(result_events) == 1
    assert result_events[0]["tool_name"] == "untrusted_tool"
    assert result_events[0]["tool_call_id"] == "call_untrusted"
    assert result_events[0]["max_tier"] == "unknown_external"
    assert result_events[0]["sources_json"][-1]["source_type"] == "tool_output"
    assert result_events[0]["sources_json"][-1]["source_id"] == "call_untrusted"


@pytest.mark.asyncio
async def test_unspecified_tool_output_defaults_to_unknown_external(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _tainting_provider()
    tracker = InMemoryTurnTaintTracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)

        await provider.execute_tool("unspecified_tool", {}, context, "call_legacy")

    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert context.tool_result_taint_metadata["call_legacy"].get("max_tier") == (
        "unknown_external"
    )
    assert "unspecified output trust" in caplog.text


@pytest.mark.asyncio
async def test_unspecified_tool_output_uses_configured_default_tier(
    db_engine: AsyncEngine,
) -> None:
    provider = TaintTrackingToolsProvider(
        _tainting_provider().wrapped_provider,
        taint_policy=TaintPolicyConfig(
            default_unspecified_tool_output_tier=SourceTrustTier.KNOWN_CONTACT,
        ),
    )
    tracker = InMemoryTurnTaintTracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)

        await provider.execute_tool("unspecified_tool", {}, context, "call_legacy")

    assert tracker.snapshot().max_tier is SourceTrustTier.KNOWN_CONTACT
    assert context.tool_result_taint_metadata["call_legacy"].get("max_tier") == (
        "known_contact"
    )


@pytest.mark.asyncio
async def test_missing_tool_output_metadata_defaults_to_unknown_external(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _tainting_provider()
    tracker = InMemoryTurnTaintTracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)

        await provider.execute_tool(
            "missing_metadata_tool",
            {},
            context,
            "call_missing_metadata",
        )

    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert (
        context.tool_result_taint_metadata["call_missing_metadata"].get("max_tier")
        == "unknown_external"
    )
    assert "no output trust metadata" in caplog.text


@pytest.mark.asyncio
async def test_attacker_addressable_egress_is_observed_before_enforcement(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    provider = _tainting_provider()
    tracker = _unknown_external_tracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)

        result = await provider.execute_tool(
            "browser_tool",
            {"url": "https://attacker.example/path", "secret": "do-not-store"},
            context,
            "call_browser",
        )
        audit_events = await db_context.taint_audit_events.list_for_turn("turn-direct")

    assert isinstance(result, ToolResult)
    assert result.get_text() == "opened url"
    assert "requested=confirm effective=audit mode=observe" in caplog.text
    would_enforce_errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and "Runtime taint WOULD ENFORCE" in record.getMessage()
    ]
    assert len(would_enforce_errors) == 1
    would_enforce_message = would_enforce_errors[0].getMessage()
    assert "would_be=confirm" in would_enforce_message
    assert "max_tier=unknown_external" in would_enforce_message
    assert "do-not-store" not in would_enforce_message
    policy_events = [
        event for event in audit_events if event["event_type"] == "policy_evaluation"
    ]
    assert len(policy_events) == 1
    policy_event = policy_events[0]
    assert policy_event["tool_name"] == "browser_tool"
    assert policy_event["sink_class"] == "attacker_addressable_egress"
    assert policy_event["requested_outcome"] == "confirm"
    assert policy_event["effective_outcome"] == "audit"
    assert policy_event["mode"] == "observe"
    assert policy_event["max_tier"] == "unknown_external"
    assert policy_event["arguments_summary_json"] == {
        "keys": ["secret", "url"],
        "value_types": {"secret": "str", "url": "str"},
    }
    assert "do-not-store" not in json.dumps(policy_event["arguments_summary_json"])


@pytest.mark.asyncio
async def test_allowed_tool_does_not_emit_would_enforce_error(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    provider = _tainting_provider()
    tracker = _unknown_external_tracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)

        result = await provider.execute_tool("home_tool", {}, context, "call_home")

    assert isinstance(result, ToolResult)
    would_enforce_errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and "Runtime taint WOULD ENFORCE" in record.getMessage()
    ]
    assert would_enforce_errors == []


@pytest.mark.asyncio
async def test_sandbox_network_after_unknown_external_is_denied_in_enforce_mode(
    db_engine: AsyncEngine,
) -> None:
    provider = TaintTrackingToolsProvider(
        _tainting_provider().wrapped_provider,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    tracker = _unknown_external_tracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)

        with pytest.raises(ToolPolicyDeniedError):
            await provider.execute_tool("worker_tool", {}, context, "call_worker")
        audit_events = await db_context.taint_audit_events.list_for_turn("turn-direct")

    policy_events = [
        event for event in audit_events if event["event_type"] == "policy_evaluation"
    ]
    assert len(policy_events) == 1
    assert policy_events[0]["tool_name"] == "worker_tool"
    assert policy_events[0]["sink_class"] == "sandbox_network"
    assert policy_events[0]["requested_outcome"] == "deny"
    assert policy_events[0]["effective_outcome"] == "deny"
    assert policy_events[0]["mode"] == "enforce"
    assert policy_events[0]["max_tier"] == "unknown_external"


@pytest.mark.asyncio
async def test_script_nested_tool_calls_recheck_current_taint(
    db_engine: AsyncEngine,
) -> None:
    provider = TaintTrackingToolsProvider(
        _tainting_provider().wrapped_provider,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    tracker = InMemoryTurnTaintTracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)
        engine = MontyEngine(tools_provider=provider)

        with pytest.raises(ScriptExecutionError, match="worker_tool"):
            await engine.evaluate_async(
                """
tools_execute("untrusted_tool")
tools_execute("worker_tool")
""",
                execution_context=context,
            )
        audit_events = await db_context.taint_audit_events.list_for_turn("turn-direct")

    policy_events = [
        event for event in audit_events if event["event_type"] == "policy_evaluation"
    ]
    assert [event["tool_name"] for event in policy_events] == [
        "untrusted_tool",
        "worker_tool",
    ]
    assert policy_events[-1]["sink_class"] == "sandbox_network"
    assert policy_events[-1]["effective_outcome"] == "deny"
    assert policy_events[-1]["max_tier"] == "unknown_external"


@pytest.mark.asyncio
async def test_redact_outcome_is_blocked_until_adapter_exists(
    db_engine: AsyncEngine,
) -> None:
    provider = TaintTrackingToolsProvider(
        _tainting_provider().wrapped_provider,
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            matrix_overrides={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ATTACKER_ADDRESSABLE_EGRESS: TaintPolicyOutcome.REDACT
                }
            },
        ),
    )
    tracker = _unknown_external_tracker()
    async with get_db_context(db_engine) as db_context:
        context = _minimal_context(db_context, tracker)

        with pytest.raises(ToolPolicyDeniedError, match="redaction outcomes"):
            await provider.execute_tool("browser_tool", {}, context, "call_browser")


@pytest.mark.asyncio
async def test_untrusted_tool_taint_persists_to_history_and_later_turn(
    db_engine: AsyncEngine,
) -> None:
    first_turn_id = "runtime-taint-turn-1"
    conversation_id = "runtime-taint-conversation"
    first_service = _processing_service(_first_call_then_final("untrusted_tool"))

    async with get_db_context(db_engine) as db_context:
        first_result = await first_service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id=conversation_id,
            trigger_content_parts=[{"type": "text", "text": "Fetch the page"}],
            trigger_interface_message_id=None,
            user_name="Test User",
            turn_id=first_turn_id,
            save_history_with_isolated_context=False,
        )

        assert first_result.status.value == "success"
        first_turn_messages = await db_context.message_history.get_by_turn_id(
            first_turn_id
        )

    tool_messages = [
        message for message in first_turn_messages if isinstance(message, ToolMessage)
    ]
    assistant_messages = [
        message
        for message in first_turn_messages
        if isinstance(message, AssistantMessage) and message.content
    ]
    assert tool_messages
    assert tool_messages[-1].taint_metadata is not None
    assert tool_messages[-1].taint_metadata.get("max_tier") == "unknown_external"
    assert assistant_messages[-1].taint_metadata is not None
    assert assistant_messages[-1].taint_metadata.get("max_tier") == "unknown_external"

    second_turn_id = "runtime-taint-turn-2"
    second_service = _processing_service(_clean_final_response("history reply"))
    async with get_db_context(db_engine) as db_context:
        second_result = await second_service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id=conversation_id,
            trigger_content_parts=[{"type": "text", "text": "Thanks"}],
            trigger_interface_message_id=None,
            user_name="Test User",
            turn_id=second_turn_id,
            save_history_with_isolated_context=False,
        )
        assert second_result.status.value == "success"
        second_turn_messages = await db_context.message_history.get_by_turn_id(
            second_turn_id
        )

    second_assistant_messages = [
        message
        for message in second_turn_messages
        if isinstance(message, AssistantMessage) and message.content
    ]
    assert second_assistant_messages[-1].taint_metadata is not None
    assert second_assistant_messages[-1].taint_metadata.get("max_tier") == (
        SourceTrustTier.UNKNOWN_EXTERNAL.config_value
    )
    history_state = TurnTaintState.from_metadata(
        second_assistant_messages[-1].taint_metadata,
        from_history=True,
    )
    assert history_state.history_high_taint_present is True


@pytest.mark.asyncio
async def test_tainted_note_write_stores_label_and_reread_restores_taint(
    db_engine: AsyncEngine,
) -> None:
    write_tracker = _unknown_external_tracker()
    async with get_db_context(db_engine) as db_context:
        write_context = _minimal_context(db_context, write_tracker)
        result = await add_or_update_note_tool(
            exec_context=write_context,
            title="External digest",
            content="Summary of external content",
        )
        assert "successfully" in result

        note = await db_context.notes.get_by_title(
            "External digest",
            visibility_grants=None,
        )
        assert note is not None
        assert note.visibility_labels == []
        assert note.provenance_metadata is not None
        assert note.provenance_metadata.get("provenance_labels") == [
            "source_unknown_external"
        ]
        taint_metadata = note.provenance_metadata.get("taint_metadata")
        assert isinstance(taint_metadata, dict)
        assert taint_metadata.get("max_tier") == "unknown_external"

    read_tracker = InMemoryTurnTaintTracker()
    async with get_db_context(db_engine) as db_context:
        read_context = _minimal_context(db_context, read_tracker)
        note_result = await get_note_tool("External digest", read_context)

    assert note_result.data is not None
    assert read_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_prompt_included_note_surfaces_stored_provenance_taint(
    db_engine: AsyncEngine,
) -> None:
    write_tracker = _unknown_external_tracker()
    async with get_db_context(db_engine) as db_context:
        write_context = _minimal_context(db_context, write_tracker)
        result = await add_or_update_note_tool(
            exec_context=write_context,
            title="Prompt external digest",
            content="External content copied into a prompt note.",
            include_in_prompt=True,
        )
        assert "successfully" in result

    async def get_context() -> Any:  # noqa: ANN401 - repository context manager
        return get_db_context(db_engine)

    provider = NotesContextProvider(get_context, prompts={})

    fragments = await provider.get_context_fragments()
    sources = await provider.get_context_taint_sources()

    assert any("Prompt external digest" in fragment for fragment in fragments)
    assert any(
        source.source_type is TaintSourceType.NOTE
        and source.source_id == "Prompt external digest"
        and source.tier is SourceTrustTier.UNKNOWN_EXTERNAL
        for source in sources
    )


@pytest.mark.asyncio
async def test_full_document_read_restores_stored_provenance_taint(
    db_engine: AsyncEngine,
) -> None:
    provenance_state = _unknown_external_tracker().snapshot()
    document = _DocumentFixture(
        source_type="email",
        source_id="message-123",
        title="External email",
        metadata={
            "source_trust_tier": "unknown_external",
            "provenance_labels": ["source_unknown_external"],
            "taint_metadata": provenance_state.to_metadata(),
        },
    )

    read_tracker = InMemoryTurnTaintTracker()
    async with get_db_context(db_engine) as db_context:
        document_id = await db_context.vector.add_document(document)
        read_context = _minimal_context(db_context, read_tracker)
        result = await get_full_document_content_tool(read_context, document_id)

    assert isinstance(result, str)
    assert "no content is available" in result
    read_state = read_tracker.snapshot()
    assert read_state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert read_state.sensitive_reads
    assert read_state.sensitive_reads[-1].scope.kind == "documents"
    assert str(document_id) in read_state.sensitive_reads[-1].scope.surfaced_ids


@pytest.mark.asyncio
async def test_text_attachment_read_restores_stored_provenance_taint(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    provenance_state = _unknown_external_tracker().snapshot()
    registry = AttachmentRegistry(
        storage_path=str(tmp_path),
        db_engine=db_engine,
        config=None,
    )
    read_tracker = InMemoryTurnTaintTracker()

    async with get_db_context(db_engine) as db_context:
        attachment = await registry.store_and_register_tool_attachment(
            file_content=b"external attachment text\n",
            filename="external.txt",
            content_type="text/plain",
            tool_name="test_tool",
            metadata={
                "source_trust_tier": "unknown_external",
                "provenance_labels": ["source_unknown_external"],
                "taint_metadata": provenance_state.to_metadata(),
            },
            db_context=db_context,
        )
        read_context = _minimal_context(
            db_context,
            read_tracker,
            attachment_registry=registry,
        )
        result = await read_text_attachment_tool(
            read_context,
            attachment.attachment_id,
        )

    assert result.text is not None
    assert "external attachment text" in result.text
    read_state = read_tracker.snapshot()
    assert read_state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert read_state.sensitive_reads
    assert read_state.sensitive_reads[-1].scope.kind == "attachments"
    assert attachment.attachment_id in read_state.sensitive_reads[-1].scope.surfaced_ids
