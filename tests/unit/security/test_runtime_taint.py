from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import NotesContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.security.taint import (
    LEGACY_MISSING_TAINT_METADATA_LABEL,
    InMemoryTurnTaintTracker,
    SinkClass,
    SourceTrustTier,
    TaintMetadata,
    TaintMetadataSource,
    TaintPolicyConfig,
    TaintPolicyEvaluator,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    amnestied_history_taint_metadata,
    merge_history_taint,
    merge_taint_policy_config,
    resolve_tool_sink_class,
    strip_legacy_labeled_echoes,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import (
    Database,
    set_engine_history_taint_epoch,
)
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.tools import LOCAL_TOOL_METADATA_BY_NAME
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
from family_assistant.tools.notes import (
    add_or_update_note_tool,
    get_note_tool,
    list_notes_tool,
)
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolExecutionContext,
    ToolResult,
)
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


async def _dynamic_taint_read_tool(exec_context: ToolExecutionContext) -> ToolResult:
    assert exec_context.taint_tracker is not None
    exec_context.taint_tracker.add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id="tainted-note",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({"source_unknown_external"}),
            reason="Stored note provenance.",
        )
    )
    return ToolResult(text="tainted note content")


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
        credential_resolvers=None,
        api_backend=None,
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


@pytest.mark.asyncio
async def test_prompt_note_taint_source_load_failure_propagates() -> None:
    class _FailingNotes:
        async def get_prompt_notes(
            self,
            *,
            visibility_grants: set[str] | None,
        ) -> list[object]:
            _ = visibility_grants
            raise RuntimeError("notes unavailable")

    class _FailingDbContext:
        notes = _FailingNotes()

    def get_context() -> _FailingDbContext:
        return _FailingDbContext()

    provider = NotesContextProvider(
        get_db_context_func=cast("Any", get_context),
        prompts={},
    )

    with pytest.raises(RuntimeError, match="notes unavailable"):
        await provider.get_context_taint_sources()


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


def test_profile_taint_policy_cannot_trust_unspecified_outputs_more_than_base() -> None:
    base = TaintPolicyConfig(
        default_unspecified_tool_output_tier=SourceTrustTier.UNKNOWN_EXTERNAL
    )
    profile = TaintPolicyConfig(
        default_unspecified_tool_output_tier=SourceTrustTier.TRUSTED_USER
    )

    with pytest.raises(
        ValueError,
        match="default_unspecified_tool_output_tier cannot be more trusted",
    ):
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


def test_profile_taint_policy_cannot_relax_redact_to_audit() -> None:
    base = TaintPolicyConfig(
        matrix_overrides={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.REDACT
            }
        }
    )
    profile = TaintPolicyConfig(
        matrix_overrides={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.AUDIT
            }
        }
    )

    with pytest.raises(ValueError, match="cannot relax base policy"):
        merge_taint_policy_config(base=base, profile=profile)


def test_profile_taint_policy_cannot_substitute_redact_for_confirm() -> None:
    base = TaintPolicyConfig(
        matrix_overrides={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.CONFIRM
            }
        }
    )
    profile = TaintPolicyConfig(
        matrix_overrides={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.REDACT
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


@pytest.mark.asyncio
async def test_legacy_history_row_missing_taint_metadata_restores_unknown_external(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    db_context = Database(db_engine)
    internal_id = await db_context.message_history.add_message(
        UserMessage(content="legacy untrusted text"),
        interface_type="test",
        conversation_id="legacy-taint",
        timestamp=datetime.now(UTC),
        turn_id="turn-legacy",
        processing_profile_id="runtime-taint-test",
    )
    assert internal_id is not None
    await db_context.execute(
        update(message_history_table)
        .where(message_history_table.c.internal_id == internal_id)
        .values(taint_metadata_json=None, taint_metadata_version=None)
    )

    rows = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id="legacy-taint",
        limit=5,
        processing_profile_id="runtime-taint-test",
    )

    assert len(rows) == 1
    state = merge_history_taint(rows)
    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present
    assert "legacy_missing_taint_metadata" in caplog.text
    legacy_records = [
        record
        for record in caplog.records
        if "legacy_missing_taint_metadata" in record.getMessage()
    ]
    assert legacy_records
    assert all(record.levelno == logging.WARNING for record in legacy_records)


_HISTORY_TAINT_EPOCH = datetime(2026, 7, 6, tzinfo=UTC)
_TEN_YEARS = timedelta(days=3650)


def _legacy_fallback_source_summary() -> TaintMetadataSource:
    return {
        "source_type": "manual",
        "source_id": "42",
        "tier": "unknown_external",
        "labels": [LEGACY_MISSING_TAINT_METADATA_LABEL],
        "reason": "Message history row predates runtime taint metadata.",
    }


def _anonymous_escalation_source_summary() -> TaintMetadataSource:
    return {
        "source_type": "manual",
        "source_id": None,
        "tier": "unknown_external",
        "labels": [],
        "reason": "Persisted taint metadata max_tier exceeded retained summaries.",
    }


def _email_source_summary() -> TaintMetadataSource:
    return {
        "source_type": "email",
        "source_id": "email-1",
        "tier": "unknown_external",
        "labels": ["source_unknown_external"],
        "reason": "Inbound email from unknown sender.",
    }


def test_amnestied_metadata_is_none_for_missing_or_malformed_metadata() -> None:
    assert amnestied_history_taint_metadata(None) is None
    assert amnestied_history_taint_metadata("not a mapping") is None
    assert amnestied_history_taint_metadata({"max_tier": "unknown_external"}) is None


def test_amnestied_metadata_drops_legacy_and_anonymous_artifacts() -> None:
    metadata = {
        "version": "runtime_v1",
        "max_tier": "unknown_external",
        "history_high_taint_present": True,
        "sources": [
            _legacy_fallback_source_summary(),
            _anonymous_escalation_source_summary(),
        ],
    }

    assert amnestied_history_taint_metadata(metadata) is None


def test_amnestied_metadata_keeps_attributed_sources_and_recomputes_tier() -> None:
    metadata = {
        "version": "runtime_v1",
        "max_tier": "unknown_external",
        "history_high_taint_present": True,
        "sources": [
            _legacy_fallback_source_summary(),
            _email_source_summary(),
        ],
    }

    result = amnestied_history_taint_metadata(metadata)

    assert result is not None
    sources = result.get("sources")
    assert sources is not None
    assert [source["source_type"] for source in sources] == ["email"]
    state = TurnTaintState.from_metadata(result, from_history=True)
    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present


def test_amnestied_metadata_does_not_honor_persisted_max_tier() -> None:
    metadata = {
        "version": "runtime_v1",
        "max_tier": "unknown_external",
        "sources": [
            {
                "source_type": "user_message",
                "source_id": "user-1",
                "tier": "trusted_user",
                "labels": [],
                "reason": "Direct user message.",
            }
        ],
    }

    result = amnestied_history_taint_metadata(metadata)

    assert result is not None
    assert result.get("max_tier") == SourceTrustTier.TRUSTED_USER.config_value
    state = TurnTaintState.from_metadata(result, from_history=True)
    assert state.max_tier is SourceTrustTier.TRUSTED_USER
    assert not state.history_high_taint_present


def test_strip_legacy_echoes_returns_metadata_unchanged_without_echoes() -> None:
    metadata: TaintMetadata = {
        "version": "runtime_v1",
        "max_tier": "unknown_external",
        "history_high_taint_present": True,
        "sources": [
            _anonymous_escalation_source_summary(),
            _email_source_summary(),
        ],
    }

    assert strip_legacy_labeled_echoes(metadata) is metadata


def test_strip_legacy_echoes_none_for_non_mapping() -> None:
    assert strip_legacy_labeled_echoes(None) is None
    assert strip_legacy_labeled_echoes("not a mapping") is None


def test_strip_legacy_echoes_drops_echo_keeps_genuine_and_recomputes() -> None:
    metadata: TaintMetadata = {
        "version": "runtime_v1",
        "max_tier": "unknown_external",
        "history_high_taint_present": True,
        "sources": [
            _legacy_fallback_source_summary(),
            _email_source_summary(),
        ],
    }

    result = strip_legacy_labeled_echoes(metadata)

    assert result is not None
    sources = result.get("sources")
    assert sources is not None
    assert [source["source_type"] for source in sources] == ["email"]
    state = TurnTaintState.from_metadata(result, from_history=True)
    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present


def test_strip_legacy_echoes_only_echoes_contributes_nothing() -> None:
    metadata: TaintMetadata = {
        "version": "runtime_v1",
        "max_tier": "unknown_external",
        "history_high_taint_present": True,
        "sources": [_legacy_fallback_source_summary()],
    }

    assert strip_legacy_labeled_echoes(metadata) is None


def test_strip_legacy_echoes_keeps_anonymous_escalation_artifact() -> None:
    metadata: TaintMetadata = {
        "version": "runtime_v1",
        "max_tier": "unknown_external",
        "history_high_taint_present": True,
        "sources": [
            _legacy_fallback_source_summary(),
            _anonymous_escalation_source_summary(),
        ],
    }

    result = strip_legacy_labeled_echoes(metadata)

    assert result is not None
    state = TurnTaintState.from_metadata(result, from_history=True)
    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present


def test_strip_legacy_echoes_preserves_hidden_persisted_max_tier() -> None:
    metadata: TaintMetadata = {
        "version": "runtime_v1",
        "max_tier": "unknown_external",
        "history_high_taint_present": True,
        "sources": [
            _legacy_fallback_source_summary(),
            {
                "source_type": "user_message",
                "source_id": "user-1",
                "tier": "trusted_user",
                "labels": [],
                "reason": "Direct user message.",
            },
        ],
    }

    result = strip_legacy_labeled_echoes(metadata)

    assert result is not None
    sources = result.get("sources")
    assert sources is not None
    assert all(
        LEGACY_MISSING_TAINT_METADATA_LABEL not in source["labels"]
        for source in sources
    )
    state = TurnTaintState.from_metadata(result, from_history=True)
    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present


def test_history_taint_epoch_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TaintPolicyConfig.model_validate({"history_taint_epoch": "2026-07-06T00:00:00"})


def test_history_taint_epoch_rejects_unparseable_values() -> None:
    with pytest.raises(ValueError, match="history_taint_epoch"):
        TaintPolicyConfig.model_validate({"history_taint_epoch": "not-a-date"})


def test_history_taint_epoch_normalizes_to_utc() -> None:
    config = TaintPolicyConfig.model_validate({
        "history_taint_epoch": "2026-07-06T02:00:00+02:00"
    })

    assert config.history_taint_epoch is not None
    assert config.history_taint_epoch == datetime(2026, 7, 6, tzinfo=UTC)
    assert config.history_taint_epoch.tzinfo == UTC


def test_profile_taint_policy_cannot_define_history_taint_epoch() -> None:
    base = TaintPolicyConfig()
    profile = TaintPolicyConfig(history_taint_epoch=_HISTORY_TAINT_EPOCH)

    with pytest.raises(ValueError, match="cannot define history_taint_epoch"):
        merge_taint_policy_config(base=base, profile=profile)


async def _seed_history_row(
    db_engine: AsyncEngine,
    *,
    conversation_id: str,
    timestamp: datetime,
    taint_metadata_json: TaintMetadata | None,
) -> int:
    db_context = Database(db_engine)
    internal_id = await db_context.message_history.add_message(
        UserMessage(content="history text"),
        interface_type="test",
        conversation_id=conversation_id,
        timestamp=timestamp,
        turn_id=str(uuid.uuid4()),
        processing_profile_id="runtime-taint-test",
    )
    assert internal_id is not None
    await db_context.execute(
        update(message_history_table)
        .where(message_history_table.c.internal_id == internal_id)
        .values(
            taint_metadata_json=taint_metadata_json,
            taint_metadata_version=(
                "runtime_v1" if taint_metadata_json is not None else None
            ),
        )
    )
    return internal_id


async def _merged_history_state(
    db_engine: AsyncEngine,
    conversation_id: str,
) -> TurnTaintState:
    db_context = Database(db_engine)
    rows = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id=conversation_id,
        limit=5,
        max_age=_TEN_YEARS,
        processing_profile_id="runtime-taint-test",
    )
    assert rows
    return merge_history_taint(rows)


@pytest.mark.asyncio
async def test_pre_epoch_row_missing_taint_metadata_contributes_no_taint(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    await _seed_history_row(
        db_engine,
        conversation_id="epoch-pre-null",
        timestamp=_HISTORY_TAINT_EPOCH - timedelta(days=1),
        taint_metadata_json=None,
    )

    state = await _merged_history_state(db_engine, "epoch-pre-null")

    assert state.max_tier is SourceTrustTier.TRUSTED_USER
    assert not state.history_high_taint_present
    assert not state.sources
    assert "legacy_missing_taint_metadata" not in caplog.text


@pytest.mark.asyncio
async def test_epoch_disabled_preserves_legacy_echo_fallback(
    db_engine: AsyncEngine,
) -> None:
    await _seed_history_row(
        db_engine,
        conversation_id="epoch-disabled-echo",
        timestamp=_HISTORY_TAINT_EPOCH + timedelta(days=1),
        taint_metadata_json={
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "history_high_taint_present": True,
            "sources": [_legacy_fallback_source_summary()],
        },
    )

    state = await _merged_history_state(db_engine, "epoch-disabled-echo")

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present


@pytest.mark.asyncio
async def test_pre_epoch_row_with_only_legacy_artifacts_contributes_no_taint(
    db_engine: AsyncEngine,
) -> None:
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    await _seed_history_row(
        db_engine,
        conversation_id="epoch-pre-poison",
        timestamp=_HISTORY_TAINT_EPOCH - timedelta(days=1),
        taint_metadata_json={
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "history_high_taint_present": True,
            "sources": [
                _legacy_fallback_source_summary(),
                _anonymous_escalation_source_summary(),
            ],
        },
    )

    state = await _merged_history_state(db_engine, "epoch-pre-poison")

    assert state.max_tier is SourceTrustTier.TRUSTED_USER
    assert not state.history_high_taint_present
    assert not state.sources


@pytest.mark.asyncio
async def test_pre_epoch_row_keeps_genuine_email_source(
    db_engine: AsyncEngine,
) -> None:
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    await _seed_history_row(
        db_engine,
        conversation_id="epoch-pre-email",
        timestamp=_HISTORY_TAINT_EPOCH - timedelta(days=1),
        taint_metadata_json={
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "history_high_taint_present": True,
            "sources": [
                _legacy_fallback_source_summary(),
                _email_source_summary(),
            ],
        },
    )

    state = await _merged_history_state(db_engine, "epoch-pre-email")

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present
    assert [source.source_type for source in state.sources] == [TaintSourceType.EMAIL]


@pytest.mark.asyncio
async def test_post_epoch_row_missing_taint_metadata_escalates_and_logs_error(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    # Unique per parametrized run: the once-per-conversation ERROR dedupe guard
    # is process-local, so a reused id would suppress the second run's record.
    conversation_id = f"epoch-post-null-{uuid.uuid4().hex}"
    await _seed_history_row(
        db_engine,
        conversation_id=conversation_id,
        timestamp=_HISTORY_TAINT_EPOCH + timedelta(days=1),
        taint_metadata_json=None,
    )

    state = await _merged_history_state(db_engine, conversation_id)

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present
    error_records = [
        record
        for record in caplog.records
        if "post_epoch_missing_taint_metadata" in record.getMessage()
    ]
    assert error_records
    assert all(record.levelno == logging.ERROR for record in error_records)
    message = error_records[0].getMessage()
    assert conversation_id in message
    assert "role=user" in message


@pytest.mark.asyncio
async def test_post_epoch_missing_metadata_error_logged_once_per_conversation(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    conversation_id = f"epoch-post-dedupe-{uuid.uuid4().hex}"
    await _seed_history_row(
        db_engine,
        conversation_id=conversation_id,
        timestamp=_HISTORY_TAINT_EPOCH + timedelta(days=1),
        taint_metadata_json=None,
    )

    await _merged_history_state(db_engine, conversation_id)
    await _merged_history_state(db_engine, conversation_id)

    error_records = [
        record
        for record in caplog.records
        if "post_epoch_missing_taint_metadata" in record.getMessage()
        and conversation_id in record.getMessage()
    ]
    assert len(error_records) == 1


@pytest.mark.asyncio
async def test_post_epoch_row_drops_legacy_echo_keeps_genuine_source(
    db_engine: AsyncEngine,
) -> None:
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    await _seed_history_row(
        db_engine,
        conversation_id="epoch-post-echo-plus-genuine",
        timestamp=_HISTORY_TAINT_EPOCH + timedelta(days=1),
        taint_metadata_json={
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "history_high_taint_present": True,
            "sources": [
                _legacy_fallback_source_summary(),
                _email_source_summary(),
            ],
        },
    )

    state = await _merged_history_state(db_engine, "epoch-post-echo-plus-genuine")

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present
    assert [source.source_type for source in state.sources] == [TaintSourceType.EMAIL]


@pytest.mark.asyncio
async def test_post_epoch_row_preserves_hidden_max_tier_after_echo_stripping(
    db_engine: AsyncEngine,
) -> None:
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    await _seed_history_row(
        db_engine,
        conversation_id="epoch-post-echo-hidden-tier",
        timestamp=_HISTORY_TAINT_EPOCH + timedelta(days=1),
        taint_metadata_json={
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "history_high_taint_present": True,
            "sources": [
                _legacy_fallback_source_summary(),
                {
                    "source_type": "user_message",
                    "source_id": "user-1",
                    "tier": "trusted_user",
                    "labels": [],
                    "reason": "Direct user message.",
                },
            ],
        },
    )

    state = await _merged_history_state(db_engine, "epoch-post-echo-hidden-tier")

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present
    assert all(
        LEGACY_MISSING_TAINT_METADATA_LABEL not in source.labels
        for source in state.sources
    )


@pytest.mark.asyncio
async def test_post_epoch_row_with_only_legacy_echo_contributes_no_taint(
    db_engine: AsyncEngine,
) -> None:
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    await _seed_history_row(
        db_engine,
        conversation_id="epoch-post-echo-only",
        timestamp=_HISTORY_TAINT_EPOCH + timedelta(days=1),
        taint_metadata_json={
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "history_high_taint_present": True,
            "sources": [_legacy_fallback_source_summary()],
        },
    )

    state = await _merged_history_state(db_engine, "epoch-post-echo-only")

    assert state.max_tier is SourceTrustTier.TRUSTED_USER
    assert not state.history_high_taint_present
    assert not state.sources


@pytest.mark.asyncio
async def test_post_epoch_row_keeps_anonymous_manual_artifact(
    db_engine: AsyncEngine,
) -> None:
    set_engine_history_taint_epoch(db_engine, _HISTORY_TAINT_EPOCH)
    await _seed_history_row(
        db_engine,
        conversation_id="epoch-post-anonymous",
        timestamp=_HISTORY_TAINT_EPOCH + timedelta(days=1),
        taint_metadata_json={
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "history_high_taint_present": True,
            "sources": [_anonymous_escalation_source_summary()],
        },
    )

    state = await _merged_history_state(db_engine, "epoch-post-anonymous")

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present
    assert [source.source_type for source in state.sources] == [TaintSourceType.MANUAL]


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
                "plain_read_only_tool",
                ToolTag.READ_ONLY,
                ToolTag.OUTPUT_TRUSTED,
            )
        )
        is SinkClass.SENSITIVE_READ_BROADENING
    )
    # An open-world read-only tool (e.g. an external search/fetch tool) can still
    # exfiltrate: the model controls the query/URL sent to the external service.
    # It must keep the egress classification, not the read-broadening class.
    assert (
        resolve_tool_sink_class(
            descriptor(
                "open_world_search",
                ToolTag.READ_ONLY,
                ToolTag.OPEN_WORLD,
                ToolTag.OUTPUT_UNTRUSTED,
            )
        )
        is SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    )
    # A read-only tool whose world is explicitly closed (no open_world tag) still
    # resolves to the read-broadening class.
    assert (
        resolve_tool_sink_class(
            descriptor(
                "closed_world_read_only",
                ToolTag.READ_ONLY,
                ToolTag.OUTPUT_TRUSTED,
            )
        )
        is SinkClass.SENSITIVE_READ_BROADENING
    )
    assert (
        resolve_tool_sink_class(descriptor("legacy_unclassified_tool"))
        is SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    )
    assert (
        resolve_tool_sink_class(
            descriptor(
                "search_provider",
                ToolTag.READ_ONLY,
                ToolTag.LOW_BANDWIDTH_EXTERNAL,
                ToolTag.OUTPUT_UNTRUSTED,
            )
        )
        is SinkClass.LOW_BANDWIDTH_EXTERNAL
    )
    assert (
        resolve_tool_sink_class(
            descriptor(
                "turn_on_light",
                ToolTag.HOME_AUTOMATION,
                ToolTag.STATE_CHANGING,
                ToolTag.OUTPUT_TRUSTED,
            )
        )
        is SinkClass.HOME_LOCAL
    )
    assert (
        resolve_tool_sink_class(
            descriptor(
                "send_ha_notification",
                ToolTag.HOME_AUTOMATION,
                ToolTag.EXTERNAL_COMM,
                ToolTag.STATE_CHANGING,
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
    assert (
        resolve_tool_sink_class(
            descriptor(
                "sensitive_automation_read",
                ToolTag.READ_ONLY,
                ToolTag.SENSITIVE_DATA,
                ToolTag.AUTOMATION,
                ToolTag.OUTPUT_TRUSTED,
            )
        )
        is SinkClass.SENSITIVE_READ_BROADENING
    )


def test_registered_tool_metadata_resolves_expected_sink_classes() -> None:
    def registered_descriptor(name: str) -> ToolDescriptor:
        metadata = LOCAL_TOOL_METADATA_BY_NAME[name]
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
            tags=metadata.tags,
            origin="local",
        )

    # mqtt_publish talks only to the operator-configured home broker; the
    # taint design doc lists "MQTT to configured broker" as home_local.
    assert (
        resolve_tool_sink_class(registered_descriptor("mqtt_publish"))
        is SinkClass.HOME_LOCAL
    )
    # Without arguments a Home Assistant action cannot be classified by domain,
    # so it keeps the conservative class. See the domain-aware tests below.
    assert (
        resolve_tool_sink_class(registered_descriptor("call_home_assistant_action"))
        is SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    )
    # Read-only tools without further classification resolve to the read
    # class instead of the arbitrary-external-message fallback.
    assert (
        resolve_tool_sink_class(registered_descriptor("jq_query"))
        is SinkClass.SENSITIVE_READ_BROADENING
    )
    # The image and video backends take a prompt with no recipient argument, so
    # the destination is fixed and they are not arbitrary external messaging.
    for fixed_destination_tool in (
        "generate_image",
        "transform_image",
        "generate_video",
    ):
        assert (
            resolve_tool_sink_class(registered_descriptor(fixed_destination_tool))
            is SinkClass.LOW_BANDWIDTH_EXTERNAL
        ), fixed_destination_tool
    # Tools whose destination the model does choose keep the arbitrary class.
    for model_addressed_tool in ("send_message_to_user", "ingest_document_from_url"):
        assert (
            resolve_tool_sink_class(registered_descriptor(model_addressed_tool))
            is SinkClass.ARBITRARY_EXTERNAL_MESSAGE
        ), model_addressed_tool


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("light", SinkClass.HOME_LOCAL),
        ("switch", SinkClass.HOME_LOCAL),
        ("climate", SinkClass.HOME_LOCAL),
        ("lock", SinkClass.HOME_LOCAL),
        # Case is normalized, so a differently-cased domain is still household.
        ("LIGHT", SinkClass.HOME_LOCAL),
        # Domains that leave the household keep the conservative class.
        ("notify", SinkClass.ARBITRARY_EXTERNAL_MESSAGE),
        ("rest_command", SinkClass.ARBITRARY_EXTERNAL_MESSAGE),
        # script and automation run operator-defined sequences that may
        # themselves notify or call out, so they are not household-local.
        ("script", SinkClass.ARBITRARY_EXTERNAL_MESSAGE),
        ("automation", SinkClass.ARBITRARY_EXTERNAL_MESSAGE),
        # play_media fetches a caller-supplied URL.
        ("media_player", SinkClass.ARBITRARY_EXTERNAL_MESSAGE),
        # These run code on the Home Assistant host.
        ("shell_command", SinkClass.SANDBOX_NETWORK),
        ("python_script", SinkClass.SANDBOX_NETWORK),
        # The allowlist fails safe: a domain it does not know is not downgraded.
        ("domain_added_by_a_future_ha_release", SinkClass.ARBITRARY_EXTERNAL_MESSAGE),
    ],
)
def test_home_assistant_action_sink_class_depends_on_domain(
    domain: str,
    expected: SinkClass,
) -> None:
    metadata = LOCAL_TOOL_METADATA_BY_NAME["call_home_assistant_action"]
    descriptor = ToolDescriptor(
        name="call_home_assistant_action",
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": "call_home_assistant_action",
                    "description": "Run a Home Assistant action.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        tags=metadata.tags,
        origin="local",
    )

    resolved = resolve_tool_sink_class(descriptor, {"domain": domain, "action": "x"})

    assert resolved is expected


def test_home_assistant_action_without_a_usable_domain_stays_conservative() -> None:
    """A malformed or absent domain must not fall through to household-local."""
    metadata = LOCAL_TOOL_METADATA_BY_NAME["call_home_assistant_action"]
    descriptor = ToolDescriptor(
        name="call_home_assistant_action",
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": "call_home_assistant_action",
                    "description": "Run a Home Assistant action.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        tags=metadata.tags,
        origin="local",
    )

    for arguments in ({"action": "turn_on"}, {"domain": None}, {"domain": 42}, {}):
        assert (
            resolve_tool_sink_class(descriptor, arguments)
            is SinkClass.ARBITRARY_EXTERNAL_MESSAGE
        ), arguments


def test_evaluate_tool_threads_arguments_into_sink_resolution() -> None:
    """The evaluator must pass call arguments through, or domains never apply."""
    metadata = LOCAL_TOOL_METADATA_BY_NAME["call_home_assistant_action"]
    descriptor = ToolDescriptor(
        name="call_home_assistant_action",
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": "call_home_assistant_action",
                    "description": "Run a Home Assistant action.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        tags=metadata.tags,
        origin="local",
    )
    evaluator = TaintPolicyEvaluator(
        TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="web_page",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Untrusted page content entered the turn.",
        )
    )

    evaluation = evaluator.evaluate_tool(
        descriptor=descriptor,
        state=state,
        arguments={"domain": "light", "action": "turn_on"},
    )

    assert evaluation.sink_class is SinkClass.HOME_LOCAL
    assert evaluation.requested_outcome is TaintPolicyOutcome.ALLOW


@pytest.mark.asyncio
async def test_tool_output_tags_update_turn_taint(
    db_engine: AsyncEngine,
) -> None:
    provider = _tainting_provider()
    tracker = InMemoryTurnTaintTracker()
    db_context = Database(db_engine)
    context = _minimal_context(db_context, tracker)

    await provider.execute_tool("trusted_tool", {}, context, "call_trusted")
    assert tracker.snapshot().max_tier is SourceTrustTier.TRUSTED_USER
    assert (
        context.tool_result_taint_metadata["call_trusted"].get("max_tier")
        == "trusted_user"
    )

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
    db_context = Database(db_engine)
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
    db_context = Database(db_engine)
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
    db_context = Database(db_engine)
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
    db_context = Database(db_engine)
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
    would_enforce_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "Runtime taint WOULD ENFORCE" in record.getMessage()
    ]
    assert len(would_enforce_warnings) == 1
    would_enforce_message = would_enforce_warnings[0].getMessage()
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
    db_context = Database(db_engine)
    context = _minimal_context(db_context, tracker)

    result = await provider.execute_tool("home_tool", {}, context, "call_home")

    assert isinstance(result, ToolResult)
    would_enforce_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "Runtime taint WOULD ENFORCE" in record.getMessage()
    ]
    assert would_enforce_warnings == []


@pytest.mark.asyncio
async def test_sandbox_network_after_unknown_external_is_denied_in_enforce_mode(
    db_engine: AsyncEngine,
) -> None:
    provider = TaintTrackingToolsProvider(
        _tainting_provider().wrapped_provider,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    tracker = _unknown_external_tracker()
    db_context = Database(db_engine)
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
    db_context = Database(db_engine)
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
    db_context = Database(db_engine)
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

    db_context = Database(db_engine)
    first_result = await first_service.handle_chat_interaction(
        db_context=db_context,
        interface_type="web",
        conversation_id=conversation_id,
        trigger_content_parts=[{"type": "text", "text": "Fetch the page"}],
        trigger_interface_message_id=None,
        user_name="Test User",
        turn_id=first_turn_id,
    )

    assert first_result.status.value == "success"
    first_turn_messages = await db_context.message_history.get_by_turn_id(first_turn_id)

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
    db_context = Database(db_engine)
    second_result = await second_service.handle_chat_interaction(
        db_context=db_context,
        interface_type="web",
        conversation_id=conversation_id,
        trigger_content_parts=[{"type": "text", "text": "Thanks"}],
        trigger_interface_message_id=None,
        user_name="Test User",
        turn_id=second_turn_id,
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
    db_context = Database(db_engine)
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
    db_context = Database(db_engine)
    read_context = _minimal_context(db_context, read_tracker)
    note_result = await get_note_tool("External digest", read_context)

    assert note_result.data is not None
    assert read_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_prompt_included_note_surfaces_stored_provenance_taint(
    db_engine: AsyncEngine,
) -> None:
    write_tracker = _unknown_external_tracker()
    db_context = Database(db_engine)
    write_context = _minimal_context(db_context, write_tracker)
    result = await add_or_update_note_tool(
        exec_context=write_context,
        title="Prompt external digest",
        content="External content copied into a prompt note.",
        include_in_prompt=True,
    )
    assert "successfully" in result

    def get_context() -> Database:
        return Database(db_engine)

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
    db_context = Database(db_engine)
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

    db_context = Database(db_engine)
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


@pytest.mark.asyncio
async def test_list_notes_preview_restores_stored_provenance_taint(
    db_engine: AsyncEngine,
) -> None:
    provenance_state = _unknown_external_tracker().snapshot()
    read_tracker = InMemoryTurnTaintTracker()

    db_context = Database(db_engine)
    await db_context.notes.add_or_update(
        title="tainted listed note",
        content="attacker preview text",
        include_in_prompt=False,
        provenance_metadata={"taint_metadata": provenance_state.to_metadata()},
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )
    read_context = _minimal_context(db_context, read_tracker)
    result = await list_notes_tool(read_context)

    assert any(note["title"] == "tainted listed note" for note in result)
    read_state = read_tracker.snapshot()
    assert read_state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert any(
        source.source_type is TaintSourceType.TOOL_OUTPUT
        and source.source_id == "external"
        for source in read_state.sources
    )


@pytest.mark.asyncio
async def test_tainted_attachment_arguments_are_merged_before_sink_policy(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    provenance_state = _unknown_external_tracker().snapshot()
    registry = AttachmentRegistry(
        storage_path=str(tmp_path),
        db_engine=db_engine,
        config=None,
    )
    provider = _tainting_provider()
    tracker = InMemoryTurnTaintTracker()

    db_context = Database(db_engine)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"external attachment text\n",
        filename="external.txt",
        content_type="text/plain",
        tool_name="test_tool",
        metadata={"taint_metadata": provenance_state.to_metadata()},
        db_context=db_context,
    )
    context = _minimal_context(
        db_context,
        tracker,
        attachment_registry=registry,
    )

    await provider.execute_tool(
        "browser_tool",
        {"attachment_ids": [attachment.attachment_id]},
        context,
        "call_browser_with_attachment",
    )
    audit_events = await db_context.taint_audit_events.list_for_turn("turn-direct")

    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    policy_events = [
        event for event in audit_events if event["event_type"] == "policy_evaluation"
    ]
    assert len(policy_events) == 1
    assert policy_events[0]["tool_name"] == "browser_tool"
    assert policy_events[0]["requested_outcome"] == "confirm"
    assert policy_events[0]["effective_outcome"] == "audit"
    assert policy_events[0]["max_tier"] == "unknown_external"


@pytest.mark.asyncio
async def test_tainted_schema_attachment_argument_without_id_name_is_merged(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    provenance_state = _unknown_external_tracker().snapshot()
    registry = AttachmentRegistry(
        storage_path=str(tmp_path),
        db_engine=db_engine,
        config=None,
    )
    provider = TaintTrackingToolsProvider(
        LocalToolsProvider(
            registrations=[
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "transform_like_tool",
                                "description": "Transform an image.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "image": {"type": "attachment"},
                                    },
                                },
                            },
                        },
                    ),
                    implementation=_browser_tool,
                    metadata=make_local_tool_metadata([
                        ToolTag.EXTERNAL_COMM,
                        ToolTag.OUTPUT_TRUSTED,
                    ]),
                )
            ]
        )
    )
    tracker = InMemoryTurnTaintTracker()

    db_context = Database(db_engine)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"external image bytes\n",
        filename="external.png",
        content_type="image/png",
        tool_name="test_tool",
        metadata={"taint_metadata": provenance_state.to_metadata()},
        db_context=db_context,
    )
    context = _minimal_context(
        db_context,
        tracker,
        attachment_registry=registry,
    )

    await provider.execute_tool(
        "transform_like_tool",
        {"image": attachment.attachment_id},
        context,
        "call_transform_like_tool",
    )
    audit_events = await db_context.taint_audit_events.list_for_turn("turn-direct")

    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    policy_events = [
        event for event in audit_events if event["event_type"] == "policy_evaluation"
    ]
    assert len(policy_events) == 1
    assert policy_events[0]["tool_name"] == "transform_like_tool"
    assert policy_events[0]["requested_outcome"] == "confirm"
    assert policy_events[0]["effective_outcome"] == "audit"


@pytest.mark.asyncio
async def test_completed_taint_confirmation_records_result_taint(
    db_engine: AsyncEngine,
) -> None:
    provider = TaintTrackingToolsProvider(
        LocalToolsProvider(
            registrations=[
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "confirmed_browser_untrusted",
                                "description": "Fetch external content after approval.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ),
                    implementation=_browser_tool,
                    metadata=make_local_tool_metadata([
                        ToolTag.BROWSER,
                        ToolTag.OUTPUT_UNTRUSTED,
                    ]),
                )
            ]
        ),
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    tracker = InMemoryTurnTaintTracker()
    tracker.add_source(
        TaintSource(
            source_type=TaintSourceType.MANUAL,
            source_id="recognized-machine",
            tier=SourceTrustTier.RECOGNIZED_MACHINE,
            labels=frozenset({"source_recognized_machine"}),
            reason="test recognized machine source",
        )
    )

    async def _completed_confirmation(
        **_kwargs: object,
    ) -> ConfirmationOutcome:
        return ConfirmationOutcome(
            kind="completed",
            result=ToolResult(text="confirmed external output"),
        )

    db_context = Database(db_engine)
    context = replace(
        _minimal_context(db_context, tracker),
        request_confirmation_callback=_completed_confirmation,
    )
    result = await provider.execute_tool(
        "confirmed_browser_untrusted",
        {},
        context,
        "call_confirmed_external",
    )
    audit_events = await db_context.taint_audit_events.list_for_turn("turn-direct")

    assert isinstance(result, ToolResult)
    assert result.text == "confirmed external output"
    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    result_taint_metadata = context.tool_result_taint_metadata[
        "call_confirmed_external"
    ]
    assert result_taint_metadata.get("max_tier") == "unknown_external"
    result_events = [
        event for event in audit_events if event["event_type"] == "result_taint"
    ]
    assert len(result_events) == 1
    assert result_events[0]["tool_name"] == "confirmed_browser_untrusted"
    assert result_events[0]["max_tier"] == "unknown_external"


@pytest.mark.asyncio
async def test_completed_taint_confirmation_merges_worker_metadata(
    db_engine: AsyncEngine,
) -> None:
    provider = TaintTrackingToolsProvider(
        LocalToolsProvider(
            registrations=[
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "confirmed_dynamic_read",
                                "description": "Read stored content after approval.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ),
                    implementation=_browser_tool,
                    metadata=make_local_tool_metadata([
                        ToolTag.SENSITIVE_DATA,
                        ToolTag.OUTPUT_TRUSTED,
                    ]),
                )
            ]
        ),
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    tracker = InMemoryTurnTaintTracker()
    tracker.add_source(
        TaintSource(
            source_type=TaintSourceType.MANUAL,
            source_id="recognized-machine",
            tier=SourceTrustTier.RECOGNIZED_MACHINE,
            labels=frozenset({"source_recognized_machine"}),
            reason="test recognized machine source",
        )
    )
    worker_taint = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id="tainted-note",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="stored note provenance",
        )
    )

    async def _completed_confirmation(
        **_kwargs: object,
    ) -> ConfirmationOutcome:
        return ConfirmationOutcome(
            kind="completed",
            result=ToolResult(text="confirmed note contents"),
            taint_metadata=worker_taint.to_metadata(),
        )

    db_context = Database(db_engine)
    context = replace(
        _minimal_context(db_context, tracker),
        request_confirmation_callback=_completed_confirmation,
    )
    result = await provider.execute_tool(
        "confirmed_dynamic_read",
        {},
        context,
        "call_confirmed_dynamic",
    )

    assert isinstance(result, ToolResult)
    assert result.text == "confirmed note contents"
    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert (
        context.tool_result_taint_metadata["call_confirmed_dynamic"].get("max_tier")
        == "unknown_external"
    )


@pytest.mark.asyncio
async def test_dynamic_provenance_added_by_trusted_read_is_persisted_on_result(
    db_engine: AsyncEngine,
) -> None:
    provider = TaintTrackingToolsProvider(
        LocalToolsProvider(
            registrations=[
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "dynamic_taint_read",
                                "description": "Read stored tainted content.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                },
                            },
                        },
                    ),
                    implementation=_dynamic_taint_read_tool,
                    metadata=make_local_tool_metadata([
                        ToolTag.READ_ONLY,
                        ToolTag.SENSITIVE_DATA,
                        ToolTag.OUTPUT_TRUSTED,
                    ]),
                )
            ]
        )
    )
    tracker = InMemoryTurnTaintTracker()

    db_context = Database(db_engine)
    context = _minimal_context(db_context, tracker)

    await provider.execute_tool(
        "dynamic_taint_read",
        {},
        context,
        "call_dynamic_read",
    )
    audit_events = await db_context.taint_audit_events.list_for_turn("turn-direct")

    assert (
        context.tool_result_taint_metadata["call_dynamic_read"].get("max_tier")
        == "unknown_external"
    )
    result_events = [
        event for event in audit_events if event["event_type"] == "result_taint"
    ]
    assert len(result_events) == 1
    assert result_events[0]["tool_name"] == "dynamic_taint_read"
    assert result_events[0]["max_tier"] == "unknown_external"
