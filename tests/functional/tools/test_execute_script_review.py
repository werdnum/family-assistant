"""Functional coverage for whole-program script review authorization."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy import update

from family_assistant.config_models import (
    AppConfig,
    KeychuteConfig,
    ToolCallReviewConfig,
    ToolsConfig,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.definition_records import (
    CreationDisposition,
    definition_content_hash,
    definition_record_from_row,
    script_definition_content,
    stamp_definition,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SinkClass,
    SourceTrustTier,
    TaintAdjudicateCell,
    TaintPolicyConfig,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.tool_call_review import (
    ToolCallReviewConstraints,
    ToolCallReviewer,
    ToolCallReviewInput,
    ToolCallReviewResult,
    ToolCallReviewStatus,
    ToolCallReviewVerdict,
)
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.storage.scripts import scripts_table
from family_assistant.task_worker import handle_script_execution
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.metadata import (
    ToolImplementation,
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolArguments,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.scripting.invocation import ScriptReviewContext


_TRUSTED_MESSAGES = (
    UserMessage(
        content="Run the requested script.",
        taint_metadata=TurnTaintState.empty().to_metadata(),
    ),
)


@dataclass
class _ReviewCall:
    review_input: ToolCallReviewInput
    constraints: ToolCallReviewConstraints
    budget_exhausted: bool


class _RecordingReviewer:
    def __init__(
        self,
        *verdicts: ToolCallReviewVerdict,
        block_call: int | None = None,
    ) -> None:
        self._verdicts = list(verdicts)
        self.block_call = block_call
        self.calls: list[_ReviewCall] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def review_tool_call(
        self,
        review_input: ToolCallReviewInput,
        constraints: ToolCallReviewConstraints,
        *,
        budget_exhausted: bool = False,
    ) -> ToolCallReviewResult:
        call_index = len(self.calls)
        self.calls.append(
            _ReviewCall(
                review_input=review_input,
                constraints=constraints,
                budget_exhausted=budget_exhausted,
            )
        )
        if self.block_call == call_index:
            self.entered.set()
            await self.release.wait()
        if call_index >= len(self._verdicts):
            raise AssertionError(f"Unexpected review call {call_index + 1}")
        verdict = self._verdicts[call_index]
        return ToolCallReviewResult(
            verdict=verdict,
            reason=f"Recording reviewer returned {verdict.value}.",
            status=ToolCallReviewStatus.MODEL_VERDICT,
            latency_ms=0,
            used_fallback=False,
        )


class _ConfirmationRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ToolArguments]] = []

    async def __call__(
        self,
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: ToolArguments,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        del (
            interface_type,
            conversation_id,
            turn_id,
            call_id,
            timeout_seconds,
            context,
        )
        self.calls.append((tool_name, tool_args))
        return ConfirmationOutcome(kind="approved")


def _real_registration(name: str) -> ToolRegistration:
    return next(
        registration
        for registration in LOCAL_TOOL_REGISTRATIONS
        if registration.name == name
    )


def _registration(
    name: str,
    implementation: ToolImplementation,
    *,
    tags: Sequence[ToolTag],
    properties: dict[str, object] | None = None,
    deterministic: bool = True,
) -> ToolRegistration:
    definition = cast(
        "ToolDefinition",
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Test tool {name}.",
                "parameters": {
                    "type": "object",
                    "properties": properties or {},
                },
            },
        },
    )
    return ToolRegistration(
        definition=definition,
        implementation=implementation,
        metadata=make_local_tool_metadata((
            *((ToolTag.SCRIPT_DETERMINISTIC,) if deterministic else ()),
            *tags,
        )),
    )


def _review_rule(
    name: str,
    decision: ToolPolicyDecision,
    *,
    priority: int = 10,
) -> PolicyRule:
    return PolicyRule(
        match=ToolMatcher(names=[name]),
        decision=decision,
        priority=priority,
        description=f"{name} test policy is {decision.value}.",
    )


def _provider(
    registrations: Sequence[ToolRegistration],
    *,
    reviewer: _RecordingReviewer | ToolCallReviewer | None,
    rules: Sequence[PolicyRule],
    default_decision: ToolPolicyDecision = ToolPolicyDecision.ALLOW,
    taint_policy: TaintPolicyConfig | None = None,
) -> TaintTrackingToolsProvider:
    local = LocalToolsProvider(registrations=registrations)
    policy = PolicyEnforcingToolsProvider(
        local,
        PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=default_decision,
                rules=list(rules),
            )
        ),
    )
    review_config = ToolCallReviewConfig(timeout_seconds=1)
    return TaintTrackingToolsProvider(
        policy,
        taint_policy=taint_policy or TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        tool_call_reviewer=cast("ToolCallReviewer | None", reviewer),
        review_config=review_config,
        include_aggregated_context=False,
    )


def _unknown_external_state() -> TurnTaintState:
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="external-message",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({"source_unknown_external"}),
            reason="External test content.",
        )
    )


def _context(
    db_engine: AsyncEngine,
    provider: TaintTrackingToolsProvider,
    *,
    state: TurnTaintState | None = None,
    confirmation: _ConfirmationRecorder | None = None,
    app_config: AppConfig | None = None,
) -> ToolExecutionContext:
    tracker = InMemoryTurnTaintTracker(state or TurnTaintState.empty())
    processing_service = SimpleNamespace(
        tools_provider=provider,
        app_config=app_config or AppConfig(),
    )
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="script-review-conversation",
        user_name="Test User",
        turn_id="script-review-turn",
        db_context=Database(db_engine),
        processing_service=cast("ProcessingService", processing_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        tools_provider=provider,
        request_confirmation_callback=confirmation,
        taint_tracker=tracker,
        taint_policy_snapshot=tracker.snapshot(),
        tool_call_review_messages=_TRUSTED_MESSAGES,
        processing_profile_id="script-review-profile",
    )


def _script_contexts(
    review_input: ToolCallReviewInput,
) -> tuple[ScriptReviewContext, ...]:
    current = (review_input.script,) if review_input.script is not None else ()
    return (*review_input.enclosing_scripts, *current)


def _assert_program_source(review_input: ToolCallReviewInput, source: str) -> None:
    assert source in {context.source for context in _script_contexts(review_input)}


async def _execute_script(
    provider: TaintTrackingToolsProvider,
    context: ToolExecutionContext,
    *,
    script: str | None = None,
    name: str | None = None,
    globals: ToolArguments | None = None,
    parameters: ToolArguments | None = None,
    call_id: str = "outer-script",
) -> str | ToolResult:
    arguments: ToolArguments = {}
    if script is not None:
        arguments["script"] = script
    if name is not None:
        arguments["name"] = name
    if globals is not None:
        arguments["globals"] = globals
    if parameters is not None:
        arguments["parameters"] = parameters
    return await provider.execute_tool(
        "execute_script",
        arguments,
        context,
        call_id,
    )


@pytest.mark.asyncio
async def test_unreviewed_outer_enriches_first_nested_review(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    source = 'ordinary_effect(value="inside")'
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("ordinary_effect", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    assert effects == ["inside"]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "ordinary_effect"
    ]
    assert reviewer.calls[0].review_input.script is None
    assert len(reviewer.calls[0].review_input.enclosing_scripts) == 1
    assert reviewer.calls[0].review_input.program_approval_requested
    _assert_program_source(reviewer.calls[0].review_input, source)


@pytest.mark.asyncio
async def test_approved_program_covers_new_taint_and_ordinary_effects_once(
    db_engine: AsyncEngine,
) -> None:
    async def read_external() -> str:
        return "runtime-derived"

    source = (
        "value = read_external()\n"
        'add_or_update_note(title="Script review", content=value)'
    )
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _real_registration("add_or_update_note"),
            _registration(
                "read_external",
                cast("ToolImplementation", read_external),
                tags=(ToolTag.READ_ONLY, ToolTag.OUTPUT_UNTRUSTED),
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            matrix_overrides={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARTIFACT_WRITE: TaintAdjudicateCell(
                        outcome=TaintPolicyOutcome.ADJUDICATE,
                        fallback=TaintPolicyOutcome.DENY,
                    )
                }
            },
        ),
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    note = await context.db_context.notes.get_by_title(
        "Script review", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None
    assert note.content == "runtime-derived"
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0].review_input.descriptor.name == "execute_script"
    reviewed_script = reviewer.calls[0].review_input.script
    assert reviewed_script is not None
    assert reviewed_script.source == source
    assert reviewed_script.inputs == {}
    assert {definition["function"]["name"] for definition in reviewed_script.tools} == {
        "execute_script",
        "add_or_update_note",
        "read_external",
    }
    assert {"llm", "llm_json", "wake_llm"}.issubset(reviewed_script.external_functions)
    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_approved_program_covers_keychute_named_sink_once(
    db_engine: AsyncEngine,
) -> None:
    request_calls = 0

    async def fake_request(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal request_calls
        request_calls += 1
        return {"status_code": 200, "headers": {}, "body": b"ok"}

    source = (
        'keychute_http_request("weather", "https://example.test/data")["status_code"]'
    )
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [_real_registration("execute_script")],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            matrix_overrides={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.SANDBOX_NETWORK: TaintAdjudicateCell(
                        outcome=TaintPolicyOutcome.ADJUDICATE,
                        fallback=TaintPolicyOutcome.DENY,
                    )
                }
            },
        ),
    )
    app_config = AppConfig(
        keychute_config=KeychuteConfig(
            enabled=True,
            url="https://keychute.test",
            token=SecretStr("test-token"),
        )
    )
    context = _context(
        db_engine,
        provider,
        state=_unknown_external_state(),
        app_config=app_config,
    )

    with patch(
        "family_assistant.scripting.apis.keychute.KeychuteScriptHttpClient.request",
        fake_request,
    ):
        result = await _execute_script(provider, context, script=source)

    assert request_calls == 1
    assert len(reviewer.calls) == 1
    assert isinstance(result, ToolResult)
    assert result.data == 200


@pytest.mark.asyncio
async def test_stored_source_is_pinned_while_review_is_pending(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    old_source = 'ordinary_effect(value="reviewed")'
    new_source = 'ordinary_effect(value="replacement")'
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW, block_call=0)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)
    await context.db_context.scripts.save(
        name="mutable-script",
        description="Stored review test",
        script_code=old_source,
        definition_taint_state=TurnTaintState.empty(),
    )

    execution = asyncio.create_task(
        _execute_script(provider, context, name="mutable-script")
    )
    await reviewer.entered.wait()
    definition_record = json.dumps(
        stamp_definition(
            content=script_definition_content(
                name="mutable-script",
                description="Stored review test",
                script_code=new_source,
                parameters_schema=None,
            ),
            taint_state=_unknown_external_state(),
        ).to_dict()
    )
    await context.db_context.execute(
        update(scripts_table)
        .where(scripts_table.c.name == "mutable-script")
        .values(
            script_code=new_source,
            definition_record=definition_record,
        )
    )
    reviewer.release.set()
    await execution

    assert effects == ["reviewed"]
    _assert_program_source(reviewer.calls[0].review_input, old_source)
    assert reviewer.calls[0].review_input.script is not None
    assert reviewer.calls[0].review_input.script.stored_name == "mutable-script"
    assert reviewer.calls[0].review_input.script.definition is not None
    assert reviewer.calls[0].review_input.script.definition.resolved


@pytest.mark.asyncio
async def test_stored_definition_imports_provenance_without_prior_sink_approval(
    db_engine: AsyncEngine,
) -> None:
    authoring_state = TurnTaintState.empty().approve_sink(
        SinkClass.SANDBOX_NETWORK,
        profile_id="coder",
    )
    current_state = TurnTaintState.empty().approve_sink(
        SinkClass.SANDBOX_NETWORK,
        profile_id="research",
    )
    provider = _provider(
        [_real_registration("execute_script")],
        reviewer=None,
        rules=[],
    )
    context = _context(db_engine, provider, state=current_state)
    await context.db_context.scripts.save(
        name="approved-in-prior-turn",
        description="Stored approval isolation test",
        script_code='print("ran")',
        definition_taint_state=authoring_state,
    )

    result = await _execute_script(
        provider,
        context,
        name="approved-in-prior-turn",
    )

    assert isinstance(result, ToolResult)
    assert context.taint_tracker is not None
    current = context.taint_tracker.snapshot()
    assert any(
        source.source_id == "script:approved-in-prior-turn"
        and source.tier is SourceTrustTier.TRUSTED_INTERNAL
        for source in current.sources
    )
    assert current.is_sink_approved(
        SinkClass.SANDBOX_NETWORK,
        profile_id="research",
    )
    assert not current.is_sink_approved(
        SinkClass.SANDBOX_NETWORK,
        profile_id="coder",
    )


@pytest.mark.asyncio
async def test_program_approval_does_not_bypass_hard_static_deny(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def forbidden_effect(value: str) -> str:
        effects.append(value)
        return "must not run"

    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "forbidden_effect",
                cast("ToolImplementation", forbidden_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            PolicyRule(
                match=ToolMatcher(
                    names=["forbidden_effect"],
                    argument_equals={"value": "blocked"},
                ),
                decision=ToolPolicyDecision.DENY,
                priority=20,
                description="The exact nested effect is denied.",
            ),
        ],
    )
    context = _context(db_engine, provider)

    result = await _execute_script(
        provider,
        context,
        script='forbidden_effect(value="blocked")',
    )

    assert effects == []
    assert len(reviewer.calls) == 1
    assert isinstance(result, ToolResult)
    assert "denied by policy" in result.get_text()


@pytest.mark.asyncio
async def test_unclassified_tool_cannot_borrow_program_approval(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def unclassified_effect(value: str) -> str:
        effects.append(value)
        return "done"

    source = 'unclassified_effect(value="inside")'
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "unclassified_effect",
                cast("ToolImplementation", unclassified_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
                deterministic=False,
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("unclassified_effect", ToolPolicyDecision.REVIEW),
        ],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    assert effects == ["inside"]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "unclassified_effect",
    ]
    _assert_program_source(reviewer.calls[1].review_input, source)
    parent = reviewer.calls[1].review_input.enclosing_scripts[-1]
    assert parent.decision == ToolCallReviewVerdict.ALLOW.value
    assert parent.review_id is not None


@pytest.mark.asyncio
async def test_program_approval_does_not_bypass_hard_static_confirmation(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def confirmed_effect(value: str) -> str:
        effects.append(value)
        return "done"

    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    confirmation = _ConfirmationRecorder()
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "confirmed_effect",
                cast("ToolImplementation", confirmed_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("confirmed_effect", ToolPolicyDecision.CONFIRM, priority=20),
        ],
    )
    context = _context(db_engine, provider, confirmation=confirmation)

    await _execute_script(
        provider,
        context,
        script='confirmed_effect(value="approved")',
    )

    assert effects == ["approved"]
    assert len(reviewer.calls) == 1
    assert confirmation.calls == [("confirmed_effect", {"value": "approved"})]


@pytest.mark.asyncio
async def test_program_approval_does_not_bypass_confirm_verdict_floor(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def floored_effect(value: str) -> str:
        effects.append(value)
        return "done"

    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    confirmation = _ConfirmationRecorder()
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "floored_effect",
                cast("ToolImplementation", floored_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            matrix_overrides={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARTIFACT_WRITE: TaintAdjudicateCell(
                        outcome=TaintPolicyOutcome.ADJUDICATE,
                        fallback=TaintPolicyOutcome.CONFIRM,
                    )
                }
            },
            operator_minimum={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARTIFACT_WRITE: TaintPolicyOutcome.CONFIRM
                }
            },
        ),
    )
    context = _context(
        db_engine,
        provider,
        state=_unknown_external_state(),
        confirmation=confirmation,
    )

    await _execute_script(
        provider,
        context,
        script='floored_effect(value="approved")',
    )

    assert effects == ["approved"]
    assert len(reviewer.calls) == 1
    assert confirmation.calls == [("floored_effect", {"value": "approved"})]


@pytest.mark.asyncio
async def test_program_approval_does_not_bypass_deny_verdict_floor(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def floored_effect(value: str) -> str:
        effects.append(value)
        return "must not run"

    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "floored_effect",
                cast("ToolImplementation", floored_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            operator_minimum={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARTIFACT_WRITE: TaintPolicyOutcome.DENY
                }
            },
        ),
    )
    context = _context(db_engine, provider, state=_unknown_external_state())

    result = await _execute_script(
        provider,
        context,
        script='floored_effect(value="blocked")',
    )

    assert effects == []
    assert len(reviewer.calls) == 1
    assert isinstance(result, ToolResult)
    assert "denied" in result.get_text().lower()


@pytest.mark.asyncio
async def test_persisted_definition_denial_is_independent_of_parent_approval(
    db_engine: AsyncEngine,
) -> None:
    async def read_external() -> str:
        return 'print("runtime-derived")'

    source = (
        "code = read_external()\n"
        'save_script(name="child", description="child", code=code)'
    )
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.DENY,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _real_registration("save_script"),
            _registration(
                "read_external",
                cast("ToolImplementation", read_external),
                tags=(ToolTag.READ_ONLY, ToolTag.OUTPUT_UNTRUSTED),
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("save_script", ToolPolicyDecision.REVIEW),
        ],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    assert await context.db_context.scripts.get_by_name("child") is None
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "save_script",
    ]
    _assert_program_source(reviewer.calls[1].review_input, source)
    parent = reviewer.calls[1].review_input.enclosing_scripts[-1]
    assert parent.decision == ToolCallReviewVerdict.ALLOW.value
    assert parent.review_id is not None
    parent = reviewer.calls[1].review_input.enclosing_scripts[-1]
    assert parent.decision == ToolCallReviewVerdict.ALLOW.value
    assert parent.review_id is not None


@pytest.mark.asyncio
async def test_persisted_definition_uses_its_own_allow_to_cure(
    db_engine: AsyncEngine,
) -> None:
    async def read_external() -> str:
        return 'print("runtime-derived")'

    source = (
        "code = read_external()\n"
        'save_script(name="child", description="child", code=code)'
    )
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _real_registration("save_script"),
            _registration(
                "read_external",
                cast("ToolImplementation", read_external),
                tags=(ToolTag.READ_ONLY, ToolTag.OUTPUT_UNTRUSTED),
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("save_script", ToolPolicyDecision.REVIEW),
        ],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    child = await context.db_context.scripts.get_by_name("child")
    assert child is not None
    record = definition_record_from_row(child.definition_record)
    assert record is not None
    # The script's own allow admitted it, which the stamp now records as the tier.
    assert record.taint_metadata.get("max_tier") == "machine_reviewed"
    assert record.disposition is CreationDisposition.JUDGE_ALLOWED
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "save_script",
    ]
    _assert_program_source(reviewer.calls[1].review_input, source)


@pytest.mark.asyncio
async def test_nested_script_starts_a_new_review_boundary(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    nested_source = 'ordinary_effect(value="nested")'
    source = f"execute_script(script={nested_source!r})"
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    assert effects == ["nested"]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "execute_script",
    ]
    _assert_program_source(reviewer.calls[0].review_input, source)
    _assert_program_source(reviewer.calls[1].review_input, nested_source)
    assert reviewer.calls[1].review_input.script is not None
    assert reviewer.calls[1].review_input.enclosing_scripts[-1].source == source


@pytest.mark.asyncio
async def test_model_result_continues_under_program_approval(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    source = 'value = llm("Choose the value")\nordinary_effect(value=value)'
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("ordinary_effect", ToolPolicyDecision.REVIEW),
        ],
    )
    context = _context(db_engine, provider)
    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content="model-derived"),
    )

    with patch(
        "family_assistant.llm.one_shot.LLMClientFactory.create_client",
        return_value=llm_client,
    ):
        await _execute_script(provider, context, script=source)

    assert effects == ["model-derived"]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
    ]


@pytest.mark.asyncio
async def test_program_authorization_does_not_leak_to_concurrent_sibling(
    db_engine: AsyncEngine,
) -> None:
    inside_entered = asyncio.Event()
    release_inside = asyncio.Event()
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        if value == "inside":
            inside_entered.set()
            await release_inside.wait()
        effects.append(value)
        return "done"

    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("ordinary_effect", ToolPolicyDecision.REVIEW),
        ],
    )
    context = _context(db_engine, provider)
    script_execution = asyncio.create_task(
        _execute_script(
            provider,
            context,
            script='ordinary_effect(value="inside")',
        )
    )
    await inside_entered.wait()

    await provider.execute_tool(
        "ordinary_effect",
        {"value": "sibling"},
        context,
        "sibling-call",
    )
    release_inside.set()
    await script_execution

    assert sorted(effects) == ["inside", "sibling"]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "ordinary_effect",
    ]


@pytest.mark.parametrize("completion", ["failure", "cancel"])
@pytest.mark.asyncio
async def test_program_authorization_expires_when_execution_stops(
    db_engine: AsyncEngine,
    completion: str,
) -> None:
    capture_entered = asyncio.Event()
    release_capture = asyncio.Event()
    captured_contexts: list[ToolExecutionContext] = []
    effects: list[str] = []

    async def capture_context(
        exec_context: ToolExecutionContext,
        block: bool,
    ) -> str:
        captured_contexts.append(exec_context)
        capture_entered.set()
        if block:
            await release_capture.wait()
        return "captured"

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "capture_context",
                cast("ToolImplementation", capture_context),
                tags=(ToolTag.READ_ONLY, ToolTag.OUTPUT_TRUSTED),
                properties={"block": {"type": "boolean"}},
            ),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("ordinary_effect", ToolPolicyDecision.REVIEW),
        ],
    )
    context = _context(db_engine, provider)

    if completion == "failure":
        result = await _execute_script(
            provider,
            context,
            script="capture_context(block=False)\n1 / 0",
        )
        assert isinstance(result, ToolResult)
        assert isinstance(result.data, dict)
        assert result.data["error_type"] == "execution_error"
    else:
        execution = asyncio.create_task(
            _execute_script(
                provider,
                context,
                script="capture_context(block=True)",
            )
        )
        await capture_entered.wait()
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

    assert len(captured_contexts) == 1
    captured_context = captured_contexts[0]
    assert captured_context.script_execution is not None
    assert not captured_context.script_execution.active

    await provider.execute_tool(
        "ordinary_effect",
        {"value": "after"},
        captured_context,
        f"after-{completion}",
    )

    assert effects == ["after"]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "ordinary_effect",
    ]
    assert reviewer.calls[1].review_input.enclosing_scripts
    assert not reviewer.calls[1].review_input.enclosing_scripts[-1].approval_active


@pytest.mark.asyncio
async def test_rejected_program_runs_no_effects(db_engine: AsyncEngine) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "must not run"

    reviewer = _RecordingReviewer(ToolCallReviewVerdict.DENY)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)

    result = await _execute_script(
        provider,
        context,
        script='ordinary_effect(value="blocked")',
    )

    assert effects == []
    assert isinstance(result, ToolResult)
    assert "blocked by automatic review" in result.get_text().lower()


@pytest.mark.asyncio
async def test_missing_reviewer_fallback_runs_no_effects(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "must not run"

    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=None,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)

    result = await _execute_script(
        provider,
        context,
        script='ordinary_effect(value="blocked")',
    )

    assert effects == []
    assert isinstance(result, ToolResult)
    assert "confirmation is required but unavailable" in result.get_text().lower()


@pytest.mark.asyncio
async def test_observe_verdict_does_not_authorize_nested_calls(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("ordinary_effect", ToolPolicyDecision.REVIEW)],
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.OBSERVE,
            matrix_overrides={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.SANDBOX_NETWORK: TaintAdjudicateCell(
                        outcome=TaintPolicyOutcome.ADJUDICATE,
                        fallback=TaintPolicyOutcome.DENY,
                    )
                }
            },
        ),
    )
    context = _context(db_engine, provider, state=_unknown_external_state())

    await _execute_script(
        provider,
        context,
        script='ordinary_effect(value="inside")',
    )
    await provider.close()

    assert effects == ["inside"]
    assert sorted(call.review_input.descriptor.name for call in reviewer.calls) == [
        "execute_script",
        "ordinary_effect",
    ]


@pytest.mark.asyncio
async def test_static_named_descendants_share_outer_review(
    db_engine: AsyncEngine,
) -> None:
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _real_registration("add_or_update_note"),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)
    child_source = 'execute_script(name="grandchild")'
    grandchild_source = 'add_or_update_note(title="Closure effect", content="executed")'
    for name, source in (("child", child_source), ("grandchild", grandchild_source)):
        await context.db_context.scripts.save(
            name=name,
            description=f"Static dependency {name}",
            script_code=source,
            definition_taint_state=TurnTaintState.empty(),
        )

    await _execute_script(provider, context, script='execute_script(name="child")')

    note = await context.db_context.notes.get_by_title(
        "Closure effect", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None
    assert note.content == "executed"
    assert len(reviewer.calls) == 1

    reviewed_script = reviewer.calls[0].review_input.script
    assert reviewed_script is not None
    bindings = {binding["name"]: binding for binding in reviewed_script.script_bindings}
    assert set(bindings) == {"child", "grandchild"}
    for name, source in (("child", child_source), ("grandchild", grandchild_source)):
        content = script_definition_content(
            name=name,
            description=f"Static dependency {name}",
            script_code=source,
            parameters_schema=None,
        )
        assert bindings[name] == {
            **content,
            "content_hash": definition_content_hash(content),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["child", "grandchild"])
async def test_static_dependency_changed_during_outer_review_cannot_run(
    db_engine: AsyncEngine,
    dependency: str,
) -> None:
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW, block_call=0)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _real_registration("add_or_update_note"),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)
    for name, source in (
        (
            "child",
            'add_or_update_note(title="Child entry effect", content="unexpected")\nexecute_script(name="grandchild")',
        ),
        ("grandchild", 'add_or_update_note(title="Stale effect", content="original")'),
    ):
        await context.db_context.scripts.save(
            name=name,
            description=f"Static dependency {name}",
            script_code=source,
            definition_taint_state=TurnTaintState.empty(),
        )
    execution = asyncio.create_task(
        _execute_script(provider, context, script='execute_script(name="child")')
    )
    await reviewer.entered.wait()
    try:
        assert await context.db_context.scripts.delete(dependency)
        await context.db_context.scripts.save(
            name=dependency,
            description="Changed while review was pending",
            script_code='add_or_update_note(title="Stale effect", content="replacement")',
            definition_taint_state=TurnTaintState.empty(),
        )
    finally:
        reviewer.release.set()
    result = await execution

    assert (
        await context.db_context.notes.get_by_title(
            "Stale effect", read_policy=NoteReadPolicy.UNRESTRICTED
        )
        is None
    )
    assert (
        await context.db_context.notes.get_by_title(
            "Child entry effect", read_policy=NoteReadPolicy.UNRESTRICTED
        )
        is None
    )
    assert len(reviewer.calls) == 1
    assert isinstance(result, ToolResult)
    assert "error" in result.get_text().lower()


@pytest.mark.asyncio
async def test_missing_static_dependency_fails_before_parent_effects(
    db_engine: AsyncEngine,
) -> None:
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _real_registration("add_or_update_note"),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)

    result = await _execute_script(
        provider,
        context,
        script='add_or_update_note(title="Parent effect", content="unexpected")\nexecute_script(name="missing-child")',
    )

    assert (
        await context.db_context.notes.get_by_title(
            "Parent effect", read_policy=NoteReadPolicy.UNRESTRICTED
        )
        is None
    )
    assert reviewer.calls == []
    assert isinstance(result, ToolResult)
    assert "missing-child" in result.get_text()


@pytest.mark.asyncio
async def test_static_child_hard_deny_survives_parent_approval(
    db_engine: AsyncEngine,
) -> None:
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _real_registration("add_or_update_note"),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            PolicyRule(
                match=ToolMatcher(
                    names=["execute_script"], argument_equals={"name": "denied-child"}
                ),
                decision=ToolPolicyDecision.DENY,
                priority=20,
            ),
        ],
    )
    context = _context(db_engine, provider)
    await context.db_context.scripts.save(
        name="denied-child",
        description="Denied dependency",
        script_code='add_or_update_note(title="Denied effect", content="unexpected")',
        definition_taint_state=TurnTaintState.empty(),
    )

    result = await _execute_script(
        provider, context, script='execute_script(name="denied-child")'
    )

    assert (
        await context.db_context.notes.get_by_title(
            "Denied effect", read_policy=NoteReadPolicy.UNRESTRICTED
        )
        is None
    )
    assert len(reviewer.calls) == 1
    assert isinstance(result, ToolResult)
    assert "denied" in result.get_text().lower()


@pytest.mark.asyncio
async def test_dynamic_named_child_requires_independent_review(
    db_engine: AsyncEngine,
) -> None:
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW, ToolCallReviewVerdict.ALLOW
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _real_registration("add_or_update_note"),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)
    child_source = 'add_or_update_note(title="Dynamic effect", content="executed")'
    await context.db_context.scripts.save(
        name="dynamic-child",
        description="Runtime selected dependency",
        script_code=child_source,
        definition_taint_state=TurnTaintState.empty(),
    )

    await _execute_script(
        provider,
        context,
        script="execute_script(name=selected_name)",
        globals={"selected_name": "dynamic-child"},
    )

    note = await context.db_context.notes.get_by_title(
        "Dynamic effect", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None
    assert note.content == "executed"
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "execute_script",
    ]
    assert reviewer.calls[1].review_input.script is not None
    assert reviewer.calls[1].review_input.script.source == child_source


def _sandbox_registration(
    effects: list[str],
    *,
    deterministic: bool = True,
) -> ToolRegistration:
    async def run_sandbox(command: str, cwd: str) -> str:
        effects.append(f"{cwd}: {command}")
        return "ran"

    return _registration(
        "run_sandbox",
        cast("ToolImplementation", run_sandbox),
        tags=(ToolTag.CODE_EXECUTION, ToolTag.WORKER, ToolTag.OUTPUT_UNTRUSTED),
        properties={"command": {"type": "string"}, "cwd": {"type": "string"}},
        deterministic=deterministic,
    )


async def _read_external() -> str:
    return "photo.png"


def _read_external_registration() -> ToolRegistration:
    return _registration(
        "read_external",
        cast("ToolImplementation", _read_external),
        tags=(ToolTag.READ_ONLY, ToolTag.OUTPUT_UNTRUSTED),
    )


@pytest.mark.asyncio
async def test_literal_sandbox_commands_inherit_program_approval(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []
    source = (
        "read_external()\n"
        'run_sandbox(command="convert in.png -dither out.bmp", cwd="/work/ink")\n'
        'run_sandbox(command="upload out.bmp", cwd="/work/ink")'
    )
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _read_external_registration(),
            _sandbox_registration(effects),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    assert effects == [
        "/work/ink: convert in.png -dither out.bmp",
        "/work/ink: upload out.bmp",
    ]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script"
    ]
    events = await context.db_context.taint_audit_events.list_for_turn(
        "script-review-turn"
    )
    program_reviews = {
        event["event_id"]
        for event in events
        if event["tool_name"] == "execute_script"
        and event["review_verdict"] == ToolCallReviewVerdict.ALLOW.value
    }
    inherited = [
        event
        for event in events
        if event["event_type"] == "script_inherited_authorization"
        and event["tool_name"] == "run_sandbox"
    ]
    assert len(inherited) == 2
    for event in inherited:
        review_context = event["review_context_json"]
        assert review_context is not None
        assert review_context.get("parent_script_review_id") in program_reviews


@pytest.mark.asyncio
async def test_runtime_built_sandbox_command_is_reviewed_without_revoking(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []
    source = (
        "name = read_external()\n"
        'run_sandbox(command=f"convert {name} out.bmp", cwd="/work/ink")\n'
        'run_sandbox(command="upload out.bmp", cwd="/work/ink")'
    )
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _read_external_registration(),
            _sandbox_registration(effects),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    assert effects == [
        "/work/ink: convert photo.png out.bmp",
        "/work/ink: upload out.bmp",
    ]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "run_sandbox",
    ]
    nested = reviewer.calls[1].review_input
    assert nested.arguments["command"] == "convert photo.png out.bmp"
    assert not nested.program_approval_requested
    assert nested.enclosing_scripts[-1].approval_active


@pytest.mark.asyncio
async def test_sandbox_without_opt_in_is_reviewed_per_call(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []
    source = (
        'run_sandbox(command="convert in.png out.bmp", cwd="/work/ink")\n'
        'run_sandbox(command="upload out.bmp", cwd="/work/ink")'
    )
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _sandbox_registration(effects, deterministic=False),
        ],
        reviewer=reviewer,
        rules=[_review_rule("execute_script", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider, state=_unknown_external_state())

    await _execute_script(provider, context, script=source)

    assert len(effects) == 2
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "run_sandbox",
        "run_sandbox",
    ]


@pytest.mark.asyncio
async def test_first_nested_review_approves_an_unreviewed_program_once(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    source = (
        "read_external()\n"
        'ordinary_effect(value="first")\n'
        'run_sandbox(command="upload out.bmp", cwd="/work/ink")\n'
        'ordinary_effect(value="second")'
    )
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _real_registration("execute_script"),
            _read_external_registration(),
            _sandbox_registration(effects),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("ordinary_effect", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    assert effects == ["first", "/work/ink: upload out.bmp", "second"]
    assert len(reviewer.calls) == 1
    first = reviewer.calls[0].review_input
    assert first.descriptor.name == "ordinary_effect"
    assert first.program_approval_requested
    _assert_program_source(first, source)


@pytest.mark.asyncio
async def test_program_review_that_does_not_allow_is_not_repeated(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    source = 'ordinary_effect(value="first")\nordinary_effect(value="second")'
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.CONFIRM,
        ToolCallReviewVerdict.ALLOW,
    )
    confirmation = _ConfirmationRecorder()
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[_review_rule("ordinary_effect", ToolPolicyDecision.REVIEW)],
    )
    context = _context(db_engine, provider, confirmation=confirmation)

    await _execute_script(provider, context, script=source)

    assert effects == ["first", "second"]
    assert [call[0] for call in confirmation.calls] == ["ordinary_effect"]
    assert [
        call.review_input.program_approval_requested for call in reviewer.calls
    ] == [True, False]
    assert not reviewer.calls[1].review_input.enclosing_scripts[-1].approval_active


@pytest.mark.asyncio
async def test_delegation_is_its_own_boundary_and_caller_resumes(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def delegate(task: str) -> str:
        effects.append(f"delegated: {task}")
        return "image-attachment"

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    source = 'image = delegate(task="draw the weather")\nordinary_effect(value=image)'
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "delegate",
                cast("ToolImplementation", delegate),
                tags=(ToolTag.DELEGATION, ToolTag.OUTPUT_UNTRUSTED),
                properties={"task": {"type": "string"}},
            ),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("delegate", ToolPolicyDecision.REVIEW),
            _review_rule("ordinary_effect", ToolPolicyDecision.REVIEW),
        ],
    )
    context = _context(db_engine, provider)

    await _execute_script(provider, context, script=source)

    assert effects == ["delegated: draw the weather", "image-attachment"]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "delegate",
    ]


@pytest.mark.asyncio
async def test_scheduled_stored_script_is_approved_by_one_program_review(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []
    source = (
        "read_external()\n"
        'run_sandbox(command="convert in.png -dither out.bmp", cwd="/work/ink")\n'
        'run_sandbox(command="upload out.bmp", cwd="/work/ink")\n'
        'run_sandbox(command="refresh-display", cwd="/work/ink")'
    )
    reviewer = _RecordingReviewer(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        [
            _read_external_registration(),
            _sandbox_registration(effects),
        ],
        reviewer=reviewer,
        rules=[],
    )
    context = _context(db_engine, provider)
    await context.db_context.scripts.save(
        name="update_eink_display",
        description="Render and upload the e-ink image.",
        script_code=source,
        definition_taint_state=TurnTaintState.empty(),
    )
    processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="unused")
        ),
        tools_provider=provider,
        service_config=ProcessingServiceConfig(
            id="script-review-profile",
            prompts={"system_prompt": "Scheduled scripts"},
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )
    context = replace(context, processing_service=processing_service)

    await handle_script_execution(
        context,
        {
            "script_name": "update_eink_display",
            "conversation_id": "script-review-conversation",
            "processing_profile_id": "script-review-profile",
        },
    )

    assert len(effects) == 3
    assert len(reviewer.calls) == 1
    review_input = reviewer.calls[0].review_input
    assert review_input.descriptor.name == "run_sandbox"
    assert review_input.program_approval_requested
    assert review_input.trigger is not None
    assert review_input.trigger.trigger_type == "scheduled_script"
    _assert_program_source(review_input, source)


@pytest.mark.asyncio
async def test_model_result_narrows_approval_for_external_destinations(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []

    async def send_external(to: str) -> str:
        effects.append(f"sent to {to}")
        return "sent"

    async def ordinary_effect(value: str) -> str:
        effects.append(value)
        return "done"

    source = (
        'send_external(to="before@example.com")\n'
        'value = llm("Summarise the inbox")\n'
        "ordinary_effect(value=value)\n"
        'send_external(to="after@example.com")'
    )
    reviewer = _RecordingReviewer(
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.ALLOW,
    )
    provider = _provider(
        [
            _real_registration("execute_script"),
            _registration(
                "send_external",
                cast("ToolImplementation", send_external),
                tags=(ToolTag.EXTERNAL_COMM, ToolTag.OUTPUT_TRUSTED),
                properties={"to": {"type": "string"}},
            ),
            _registration(
                "ordinary_effect",
                cast("ToolImplementation", ordinary_effect),
                tags=(ToolTag.STATE_CHANGING, ToolTag.OUTPUT_TRUSTED),
                properties={"value": {"type": "string"}},
            ),
        ],
        reviewer=reviewer,
        rules=[
            _review_rule("execute_script", ToolPolicyDecision.REVIEW),
            _review_rule("send_external", ToolPolicyDecision.REVIEW),
            _review_rule("ordinary_effect", ToolPolicyDecision.REVIEW),
        ],
    )
    context = _context(db_engine, provider)
    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content="model-derived"),
    )

    with patch(
        "family_assistant.llm.one_shot.LLMClientFactory.create_client",
        return_value=llm_client,
    ):
        await _execute_script(provider, context, script=source)

    assert effects == [
        "sent to before@example.com",
        "model-derived",
        "sent to after@example.com",
    ]
    assert [call.review_input.descriptor.name for call in reviewer.calls] == [
        "execute_script",
        "send_external",
    ]
    assert reviewer.calls[1].review_input.arguments["to"] == "after@example.com"
