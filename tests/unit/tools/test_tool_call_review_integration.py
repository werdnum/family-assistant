"""Runtime integration tests for the central tool-call review path."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import (
    ToolCallReviewConfig,
    ToolCallReviewEscalationConfig,
)
from family_assistant.llm.messages import AssistantMessage, SystemMessage, UserMessage
from family_assistant.security.definition_records import (
    CreationDisposition,
    DefinitionGateOutcome,
    GateLayer,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SensitiveReadScope,
    SinkClass,
    SourceTrustTier,
    TaintPolicyCell,
    TaintPolicyConfig,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.confirmation_service import ConfirmationService
from family_assistant.services.deferred_tool_confirmation import (
    DeferredConfirmationCallbackAdapter,
    build_deferred_confirmation_callback,
)
from family_assistant.services.tool_call_review import (
    ToolCallReviewer,
    ToolCallReviewResponse,
    ToolCallReviewStatus,
    ToolCallReviewVerdict,
    TriggerReviewInput,
)
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    TaintTrackingToolsProvider,
    ToolPolicyDeniedError,
)
from family_assistant.tools.metadata import (
    ToolImplementation,
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.taint_helpers import record_sensitive_read
from family_assistant.tools.types import (
    ConfirmationOutcome,
    RequestConfirmationCallback,
    ToolCallReviewAuthorization,
    ToolExecutionContext,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools.types import ToolArguments, ToolDefinition


# These waits guard against a hang, not a deadline: the assertions around them
# are what check ordering. A budget tight enough to expire on a loaded machine
# turns a passing test into a flake without testing anything more.
_HANG_GUARD_SECONDS = 30


class _ReviewLLM:
    def __init__(
        self,
        verdict: ToolCallReviewVerdict,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        raise_timeout: bool = False,
        reason: str | None = None,
    ) -> None:
        self.verdict = verdict
        self.entered = entered
        self.release = release
        self.raise_timeout = raise_timeout
        self.reason = reason
        self.calls = 0
        self.last_messages: Sequence[LLMMessage] | None = None

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        self.last_messages = tuple(messages)
        assert response_model is ToolCallReviewResponse
        assert max_retries == 0
        self.calls += 1
        if self.raise_timeout:
            raise TimeoutError
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return cast(
            "T",
            ToolCallReviewResponse(
                verdict=self.verdict,
                reason=self.reason or f"Reviewer chose {self.verdict.value}.",
                safer_alternative="Use a local-only operation."
                if self.verdict is ToolCallReviewVerdict.DENY
                else None,
            ),
        )


class _ConfirmationRecorder:
    def __init__(self, outcome: ConfirmationOutcome | None = None) -> None:
        self.calls = 0
        self.review_reasons: list[str | None] = []
        self.outcome = outcome or ConfirmationOutcome(kind="approved")

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
            tool_name,
            call_id,
            tool_args,
            timeout_seconds,
        )
        self.calls += 1
        self.review_reasons.append(context.tool_call_review_confirmation_reason)
        return self.outcome


class _BlockingConfirmationRecorder(_ConfirmationRecorder):
    def __init__(self, outcome: ConfirmationOutcome | None = None) -> None:
        super().__init__(outcome)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

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
        self.calls += 1
        self.review_reasons.append(context.tool_call_review_confirmation_reason)
        self.entered.set()
        await self.release.wait()
        return self.outcome


class _DecisionOnlyConfirmationManager:
    def __init__(self, outcome: ConfirmationOutcome | None = None) -> None:
        self.calls = 0
        self.requests: list[dict[str, object]] = []
        self.outcome = outcome or ConfirmationOutcome(kind="approved")

    async def request_confirmation(self, **kwargs: object) -> ConfirmationOutcome:
        self.calls += 1
        self.requests.append(kwargs)
        return self.outcome


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


def _nested_sink_arguments(
    *,
    url: str = "https://example.test/v1/items",
    secret: str = "secret-a",
    method: str = "POST",
    reordered: bool = False,
) -> ToolArguments:
    """Return JSON-like sink args containing nested, unhashable containers."""
    if reordered:
        return {
            "payload": [{"metadata": {"enabled": True}, "id": 1}],
            "auth": {
                "scopes": ["write", {"tenant": "home"}],
                "secret": secret,
            },
            "method": method,
            "url": url,
        }
    return {
        "url": url,
        "method": method,
        "auth": {
            "secret": secret,
            "scopes": ["write", {"tenant": "home"}],
        },
        "payload": [{"id": 1, "metadata": {"enabled": True}}],
    }


def _context(
    db_engine: AsyncEngine,
    state: TurnTaintState,
    *,
    confirmation: RequestConfirmationCallback | None = None,
    turn_id: str = "review-integration-turn",
) -> ToolExecutionContext:
    tracker = InMemoryTurnTaintTracker(state)
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="review-integration-conversation",
        user_name="Test User",
        turn_id=turn_id,
        db_context=Database(db_engine),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        processing_profile_id="review-integration-profile",
        request_confirmation_callback=confirmation,
        taint_tracker=tracker,
        taint_policy_snapshot=state,
    )


def _registration(
    implementation: ToolImplementation,
    *,
    tool_name: str = "reviewed_tool",
    browser: bool = False,
    sandbox: bool = False,
    delegation: bool = False,
    output_untrusted: bool = False,
    sensitive_read: bool = False,
    deferred_confirmation_eligible: bool = False,
) -> ToolRegistration:
    if browser:
        tags = (ToolTag.BROWSER, ToolTag.EXTERNAL_COMM, ToolTag.OUTPUT_UNTRUSTED)
    elif sandbox:
        tags = (ToolTag.WORKER, ToolTag.CODE_EXECUTION, ToolTag.OUTPUT_TRUSTED)
    elif delegation:
        tags = (ToolTag.DELEGATION, ToolTag.OUTPUT_TRUSTED)
    elif output_untrusted:
        tags = (ToolTag.EXTERNAL_COMM, ToolTag.OUTPUT_UNTRUSTED)
    elif sensitive_read:
        tags = (ToolTag.READ_ONLY, ToolTag.SENSITIVE_DATA, ToolTag.OUTPUT_TRUSTED)
    else:
        tags = (ToolTag.EXTERNAL_COMM, ToolTag.OUTPUT_TRUSTED)
    return ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": "Perform the reviewed test operation.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "destination": {"type": "string"},
                        },
                    },
                },
            },
        ),
        implementation=implementation,
        metadata=make_local_tool_metadata(
            tags,
            destination_argument_paths=("destination",),
            deferred_confirmation_eligible=deferred_confirmation_eligible,
        ),
    )


def _provider(
    implementation: ToolImplementation,
    *,
    tool_name: str = "reviewed_tool",
    reviewer_llm: _ReviewLLM | None,
    static_decision: ToolPolicyDecision,
    taint_policy: TaintPolicyConfig,
    include_aggregated_context: bool | None = True,
    browser: bool = False,
    sandbox: bool = False,
    delegation: bool = False,
    output_untrusted: bool = False,
    sensitive_read: bool = False,
    deferred_confirmation_eligible: bool = False,
    review_config: ToolCallReviewConfig | None = None,
) -> TaintTrackingToolsProvider:
    local = LocalToolsProvider(
        registrations=[
            _registration(
                implementation,
                tool_name=tool_name,
                browser=browser,
                sandbox=sandbox,
                delegation=delegation,
                output_untrusted=output_untrusted,
                sensitive_read=sensitive_read,
                deferred_confirmation_eligible=deferred_confirmation_eligible,
            )
        ]
    )
    policy = PolicyEnforcingToolsProvider(
        local,
        PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=static_decision)
        ),
    )
    review_config = review_config or ToolCallReviewConfig(timeout_seconds=1)
    reviewer = (
        ToolCallReviewer(cast("LLMInterface", reviewer_llm), review_config)
        if reviewer_llm is not None
        else None
    )
    return TaintTrackingToolsProvider(
        policy,
        taint_policy=taint_policy,
        tool_call_reviewer=reviewer,
        review_config=review_config,
        include_aggregated_context=include_aggregated_context,
    )


def _sensitive_read_and_egress_provider(
    sensitive_implementation: ToolImplementation,
    egress_implementation: ToolImplementation,
    reviewer_llm: _ReviewLLM,
) -> TaintTrackingToolsProvider:
    local = LocalToolsProvider(
        registrations=[
            _registration(
                sensitive_implementation,
                tool_name="get_note",
                sensitive_read=True,
            ),
            _registration(
                egress_implementation,
                tool_name="send_external",
            ),
        ]
    )
    policy = PolicyEnforcingToolsProvider(
        local,
        PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )
    review_config = ToolCallReviewConfig(timeout_seconds=1)
    return TaintTrackingToolsProvider(
        policy,
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            matrix_overrides={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.SENSITIVE_READ_BROADENING: TaintPolicyOutcome.AUDIT,
                    SinkClass.ARBITRARY_EXTERNAL_MESSAGE: (
                        TaintPolicyOutcome.ADJUDICATE
                    ),
                }
            },
        ),
        tool_call_reviewer=ToolCallReviewer(
            cast("LLMInterface", reviewer_llm), review_config
        ),
        review_config=review_config,
        include_aggregated_context=False,
    )


async def _review_events(
    context: ToolExecutionContext,
) -> list[dict[str, object]]:
    assert context.turn_id is not None
    events = await context.db_context.taint_audit_events.list_for_turn(context.turn_id)
    return [
        cast("dict[str, object]", event)
        for event in events
        if event["event_type"] == "tool_call_review"
    ]


async def test_unattended_trigger_reaches_reviewer_as_provenance_stub(
    db_engine: AsyncEngine,
) -> None:
    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="executed")

    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    context = _context(db_engine, TurnTaintState.empty())
    context.tool_call_review_trigger = TriggerReviewInput(
        trigger_type="event_listener",
        active_request_role="user",
        definition="UNRENDERED_TRIGGER_DEFINITION",
        definition_taint_metadata=None,
        payload_present=True,
    )
    context.tool_call_review_messages = (
        UserMessage(
            content="Earlier interactive request mentioned friend@example.test.",
            taint_metadata=TurnTaintState.empty().to_metadata(),
        ),
        UserMessage(
            content="Current callback payload mentions friend@example.test.",
            taint_metadata=_unknown_external_state().to_metadata(),
        ),
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {"destination": "friend@example.test"},
        context,
        "trigger-call",
    )

    assert isinstance(result, ToolResult)
    assert result.get_text() == "executed"
    assert llm.last_messages is not None
    prompt = "\n".join(str(message.content) for message in llm.last_messages)
    assert "<trigger_definition_stub>" in prompt
    assert "event_listener definition, tier missing" in prompt
    assert "UNRENDERED_TRIGGER_DEFINITION" not in prompt
    assert "Earlier interactive request mentioned friend@example.test." not in prompt
    assert "Current callback payload mentions friend@example.test." not in prompt
    assert "<trigger_payload_stub>" in prompt
    assert (
        "Destination appears nowhere in the current trusted request or in an "
        "attested trigger definition." in prompt
    )
    reviews = await _review_events(context)
    assert len(reviews) == 1
    review_context = reviews[0]["review_context_json"]
    assert isinstance(review_context, dict)
    assert review_context["destination_echo"] is False


@pytest.mark.parametrize(
    ("verdict", "expected_executions", "expected_confirmations"),
    [
        (ToolCallReviewVerdict.ALLOW, 1, 0),
        (ToolCallReviewVerdict.DENY, 0, 0),
        (ToolCallReviewVerdict.CONFIRM, 1, 1),
    ],
)
async def test_static_review_and_taint_adjudicate_share_one_judgment(
    db_engine: AsyncEngine,
    verdict: ToolCallReviewVerdict,
    expected_executions: int,
    expected_confirmations: int,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed")

    llm = _ReviewLLM(verdict)
    confirmation = _ConfirmationRecorder()
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    context = _context(db_engine, _unknown_external_state(), confirmation=confirmation)

    result = await provider.execute_tool(
        "reviewed_tool",
        {"destination": "friend@example.test"},
        context,
        "combined-review-call",
    )

    assert llm.calls == 1
    assert executions == expected_executions
    assert confirmation.calls == expected_confirmations
    if verdict is ToolCallReviewVerdict.CONFIRM:
        assert confirmation.review_reasons == ["Reviewer chose confirm."]
    assert len(await _review_events(context)) == 1
    if verdict is ToolCallReviewVerdict.DENY:
        assert isinstance(result, ToolResult)
        assert "Action blocked by automatic review" in result.get_text()
        assert "Reviewer chose deny." in result.get_text()
        assert "Safer alternative" in result.get_text()
    else:
        assert isinstance(result, ToolResult)
        assert result.get_text() == "executed"


async def test_live_review_confirmation_is_scoped_to_inner_delegation_gate(
    db_engine: AsyncEngine,
) -> None:
    observed_authorizations: list[tuple[str, str, ToolArguments, bool]] = []

    async def execute(
        exec_context: ToolExecutionContext,
        destination: str,
    ) -> ToolResult:
        authorization = exec_context.tool_confirmation_authorization
        assert authorization is not None
        observed_authorizations.append((
            authorization.tool_name,
            authorization.call_id,
            authorization.tool_args,
            authorization.consumed,
        ))
        assert destination == "target-profile"
        return ToolResult(text="delegated")

    confirmation = _ConfirmationRecorder()
    provider = _provider(
        cast("ToolImplementation", execute),
        tool_name="delegate_to_service",
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.CONFIRM),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        delegation=True,
    )
    context = _context(db_engine, TurnTaintState.empty(), confirmation=confirmation)

    result = await provider.execute_tool(
        "delegate_to_service",
        {"destination": "target-profile"},
        context,
        "reviewed-delegation-call",
    )

    assert isinstance(result, ToolResult)
    assert result.get_text() == "delegated"
    assert confirmation.calls == 1
    assert observed_authorizations == [
        (
            "delegate_to_service",
            "reviewed-delegation-call",
            {"destination": "target-profile"},
            True,
        )
    ]
    assert context.tool_confirmation_authorization is None


async def test_eligible_unattended_review_confirmation_creates_durable_request(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.CONFIRM),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        deferred_confirmation_eligible=True,
    )
    deferred_confirmation = build_deferred_confirmation_callback(
        target_user_id="owner-user",
        source_prefix="From an unattended review — approve to run:",
        missing_owner_message=lambda tool_name: f"No owner for {tool_name}",
    )
    context = _context(
        db_engine,
        TurnTaintState.empty(),
        confirmation=deferred_confirmation,
        turn_id="eligible-unattended-review",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {"destination": "friend@example.test"},
        context,
        "eligible-unattended-call",
    )

    assert isinstance(result, str)
    assert "Waiting on the user to approve" in result
    assert executions == 0
    pending = await ConfirmationService(db=context.db_context).list_pending_for_user(
        user_id="owner-user"
    )
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "reviewed_tool"
    assert pending[0]["tool_call_id"] == "eligible-unattended-call"
    assert pending[0]["static_policy_reason"] is not None


async def test_ineligible_unattended_review_confirmation_denies_without_deferral(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="must not execute")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.CONFIRM),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    deferred_confirmation = build_deferred_confirmation_callback(
        target_user_id="owner-user",
        source_prefix="From an unattended review — approve to run:",
        missing_owner_message=lambda tool_name: f"No owner for {tool_name}",
    )
    context = _context(
        db_engine,
        TurnTaintState.empty(),
        confirmation=deferred_confirmation,
        turn_id="ineligible-unattended-review",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {"destination": "friend@example.test"},
        context,
        "ineligible-unattended-call",
    )

    assert isinstance(result, ToolResult)
    assert "Action blocked by automatic review" in result.get_text()
    assert "result is not independent and terminal" in result.get_text()
    assert executions == 0
    pending = await ConfirmationService(db=context.db_context).list_pending_for_user(
        user_id="owner-user"
    )
    assert pending == []


async def test_hard_static_confirm_keeps_existing_unattended_deferral(
    db_engine: AsyncEngine,
) -> None:
    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="must execute only after approval")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=None,
        static_decision=ToolPolicyDecision.CONFIRM,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    deferred_confirmation = build_deferred_confirmation_callback(
        target_user_id="owner-user",
        source_prefix="From an unattended hard gate — approve to run:",
        missing_owner_message=lambda tool_name: f"No owner for {tool_name}",
    )
    context = _context(
        db_engine,
        TurnTaintState.empty(),
        confirmation=deferred_confirmation,
        turn_id="hard-confirm-unattended",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {"destination": "friend@example.test"},
        context,
        "hard-confirm-unattended-call",
    )

    assert isinstance(result, str)
    assert "Waiting on the user to approve" in result
    pending = await ConfirmationService(db=context.db_context).list_pending_for_user(
        user_id="owner-user"
    )
    assert len(pending) == 1
    assert pending[0]["tool_call_id"] == "hard-confirm-unattended-call"
    assert pending[0]["static_policy_reason"] is None


async def test_adapted_deferred_placeholder_does_not_add_tool_output_taint(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="must only execute after approval")

    async def queue_confirmation(
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
            tool_name,
            call_id,
            tool_args,
            timeout_seconds,
            context,
        )
        return ConfirmationOutcome(kind="completed", result="approval queued")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=None,
        static_decision=ToolPolicyDecision.CONFIRM,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        output_untrusted=True,
    )
    context = _context(
        db_engine,
        TurnTaintState.empty(),
        confirmation=DeferredConfirmationCallbackAdapter(queue_confirmation),
        turn_id="adapted-deferred-placeholder",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {"destination": "friend@example.test"},
        context,
        "adapted-deferred-call",
    )

    assert result == "approval queued"
    assert executions == 0
    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot() == TurnTaintState.empty()
    assert (
        TurnTaintState.from_metadata(
            context.tool_result_taint_metadata["adapted-deferred-call"]
        )
        == TurnTaintState.empty()
    )


async def test_observe_taint_deny_floor_does_not_constrain_static_review(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed from static review")

    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.OBSERVE,
            operator_minimum={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.DENY
                }
            },
        ),
    )
    context = _context(db_engine, _unknown_external_state())

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "observe-deny-floor-static-review",
    )

    assert isinstance(result, ToolResult)
    assert result.get_text() == "executed from static review"
    assert executions == 1
    assert llm.calls == 1
    events = await _review_events(context)
    assert len(events) == 1
    assert events[0]["mode"] == TaintPolicyMode.OBSERVE.value
    review_context = events[0]["review_context_json"]
    assert isinstance(review_context, dict)
    assert review_context["allowed_verdicts"] == [
        ToolCallReviewVerdict.ALLOW.value,
        ToolCallReviewVerdict.CONFIRM.value,
        ToolCallReviewVerdict.DENY.value,
    ]
    assert review_context["fallback_verdict"] == ToolCallReviewVerdict.CONFIRM.value


async def test_observe_taint_only_deny_floor_constrains_shadow_review(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed before shadow verdict")

    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.OBSERVE,
            operator_minimum={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.DENY
                }
            },
        ),
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="observe-taint-only-deny-floor",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "observe-taint-only-deny-floor-call",
    )
    await provider.close()

    assert isinstance(result, ToolResult)
    assert result.get_text() == "executed before shadow verdict"
    assert executions == 1
    assert llm.calls == 1
    events = await _review_events(context)
    assert len(events) == 1
    assert events[0]["review_verdict"] == ToolCallReviewVerdict.DENY.value
    assert events[0]["review_status"] == ToolCallReviewStatus.MALFORMED_FALLBACK.value
    assert events[0]["mode"] == TaintPolicyMode.OBSERVE.value
    review_context = events[0]["review_context_json"]
    assert isinstance(review_context, dict)
    assert review_context["allowed_verdicts"] == [ToolCallReviewVerdict.DENY.value]
    assert review_context["fallback_verdict"] == ToolCallReviewVerdict.DENY.value
    assert context.tool_call_review_state.consecutive_denials == 0
    assert context.tool_call_review_state.total_denials == 0


async def test_observe_taint_only_timeout_keeps_sandbox_deny_fallback(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed before shadow timeout")

    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW, raise_timeout=True)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.OBSERVE),
        sandbox=True,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="observe-taint-only-sandbox-fallback",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "observe-taint-only-sandbox-fallback-call",
    )
    await provider.close()

    assert isinstance(result, ToolResult)
    assert result.get_text() == "executed before shadow timeout"
    assert executions == 1
    assert llm.calls == 1
    events = await _review_events(context)
    assert len(events) == 1
    assert events[0]["review_verdict"] == ToolCallReviewVerdict.DENY.value
    assert events[0]["review_status"] == ToolCallReviewStatus.TIMEOUT_FALLBACK.value
    assert events[0]["mode"] == TaintPolicyMode.OBSERVE.value
    review_context = events[0]["review_context_json"]
    assert isinstance(review_context, dict)
    assert review_context["allowed_verdicts"] == [
        ToolCallReviewVerdict.ALLOW.value,
        ToolCallReviewVerdict.CONFIRM.value,
        ToolCallReviewVerdict.DENY.value,
    ]
    assert review_context["fallback_verdict"] == ToolCallReviewVerdict.DENY.value
    assert context.tool_call_review_state.consecutive_denials == 0
    assert context.tool_call_review_state.total_denials == 0


async def test_observe_taint_deny_fallback_does_not_override_static_fallback(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed after static fallback confirmation")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY, raise_timeout=True)
    confirmation = _ConfirmationRecorder()
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.OBSERVE,
            operator_minimum={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.DENY
                }
            },
        ),
    )
    context = _context(db_engine, _unknown_external_state(), confirmation=confirmation)

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "observe-deny-fallback-static-review",
    )

    assert isinstance(result, ToolResult)
    assert result.get_text() == "executed after static fallback confirmation"
    assert executions == 1
    assert llm.calls == 1
    assert confirmation.calls == 1
    events = await _review_events(context)
    assert len(events) == 1
    assert events[0]["review_verdict"] == ToolCallReviewVerdict.CONFIRM.value
    assert events[0]["review_status"] == ToolCallReviewStatus.TIMEOUT_FALLBACK.value
    assert events[0]["mode"] == TaintPolicyMode.OBSERVE.value
    review_context = events[0]["review_context_json"]
    assert isinstance(review_context, dict)
    assert review_context["allowed_verdicts"] == [
        ToolCallReviewVerdict.ALLOW.value,
        ToolCallReviewVerdict.CONFIRM.value,
        ToolCallReviewVerdict.DENY.value,
    ]
    assert review_context["fallback_verdict"] == ToolCallReviewVerdict.CONFIRM.value


async def test_observe_taint_review_is_nonblocking_and_close_drains_audit(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed before shadow verdict")

    entered = asyncio.Event()
    release = asyncio.Event()
    llm = _ReviewLLM(
        ToolCallReviewVerdict.DENY,
        entered=entered,
        release=release,
    )
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.OBSERVE),
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="shadow-review-turn",
    )

    result = await provider.execute_tool("reviewed_tool", {}, context, "shadow-call")

    assert isinstance(result, ToolResult)
    assert result.get_text() == "executed before shadow verdict"
    assert executions == 1
    assert not release.is_set()
    await asyncio.wait_for(entered.wait(), timeout=_HANG_GUARD_SECONDS)
    assert await _review_events(context) == []

    release.set()
    await provider.close()

    events = await _review_events(context)
    assert len(events) == 1
    assert events[0]["review_verdict"] == ToolCallReviewVerdict.DENY.value
    assert events[0]["review_status"] == ToolCallReviewStatus.MODEL_VERDICT.value
    audit_reason = events[0]["reason"]
    assert isinstance(audit_reason, str)
    assert audit_reason == (
        "Automatic reviewer decision recorded; reviewer rationale omitted from "
        "durable audit."
    )
    assert "Reviewer chose deny." not in audit_reason
    assert events[0]["tool_call_id"] == "shadow-call"
    review_context = events[0]["review_context_json"]
    assert isinstance(review_context, dict)
    assert review_context["destination_echo"] is None


async def test_review_audit_omits_rationale_and_raw_evidence(
    db_engine: AsyncEngine,
) -> None:
    """The complete audit row excludes raw arguments, provenance, and prompt data."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="must not execute")

    raw_destination = "TOP_SECRET_DESTINATION@example.test"
    raw_prompt_token = "PROMPT_SECRET_PHRASE"
    raw_argument_key = "DYNAMIC_SECRET_ARGUMENT_KEY"
    raw_nested_key = "NESTED_SECRET_MAPPING_KEY"
    raw_nested_value = "NESTED_SECRET_MAPPING_VALUE"
    raw_source_id = "UNTRUSTED_SECRET_SOURCE_ID"
    raw_source_label = "UNTRUSTED_SECRET_SOURCE_LABEL"
    raw_source_reason = "UNTRUSTED_SECRET_SOURCE_REASON"
    state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id=raw_source_id,
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({raw_source_label}),
            reason=raw_source_reason,
        )
    )
    llm = _ReviewLLM(
        ToolCallReviewVerdict.DENY,
        reason=(
            f"The destination argument targets {raw_destination}. "
            f"Block because {raw_prompt_token}, {raw_argument_key}, "
            f"{raw_nested_key}, and {raw_nested_value} are unrelated to the "
            f"configured workflow. Source {raw_source_id} has {raw_source_label}: "
            f"{raw_source_reason}."
        ),
    )
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    context = _context(
        db_engine,
        state,
        turn_id="sanitized-review-reason",
    )
    context.tool_call_review_messages = (
        UserMessage(
            content=f"{raw_prompt_token} instructs a sensitive action.",
            taint_metadata=state.to_metadata(),
        ),
    )

    await provider.execute_tool(
        "reviewed_tool",
        {
            "destination": raw_destination,
            raw_argument_key: {raw_nested_key: raw_nested_value},
        },
        context,
        "sanitized-review-call",
    )

    events = await _review_events(context)
    assert len(events) == 1
    audit_reason = events[0]["reason"]
    assert isinstance(audit_reason, str)
    assert audit_reason == (
        "Automatic reviewer decision recorded; reviewer rationale omitted from "
        "durable audit."
    )
    assert "destination argument" not in audit_reason
    assert events[0]["tool_call_id"] == "sanitized-review-call"

    arguments_summary = events[0]["arguments_summary_json"]
    assert isinstance(arguments_summary, dict)
    assert "destination" in arguments_summary["keys"]
    assert any(key.startswith("argument_") for key in arguments_summary["keys"])
    sources = events[0]["sources_json"]
    assert sources == [
        {
            "source_type": "email",
            "source_id": None,
            "tier": "unknown_external",
            "labels": [],
            "reason": "Externally authored source details omitted from audit.",
        }
    ]

    serialized_event = json.dumps(events[0], default=str, sort_keys=True)
    for secret in (
        raw_destination,
        raw_prompt_token,
        raw_argument_key,
        raw_nested_key,
        raw_nested_value,
        raw_source_id,
        raw_source_label,
        raw_source_reason,
    ):
        assert secret not in serialized_event


async def test_missing_reviewer_keeps_unknown_external_sandbox_fallback_deny(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="must not execute")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=None,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        sandbox=True,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="sandbox-fallback-turn",
    )

    result = await provider.execute_tool("reviewed_tool", {}, context, "sandbox-call")

    assert executions == 0
    assert isinstance(result, ToolResult)
    assert "Action blocked by automatic review" in result.get_text()
    events = await _review_events(context)
    assert len(events) == 1
    assert events[0]["review_verdict"] == ToolCallReviewVerdict.DENY.value
    assert events[0]["review_status"] == ToolCallReviewStatus.DISABLED_FALLBACK.value


async def test_terminal_review_denial_records_trusted_result_metadata(
    db_engine: AsyncEngine,
) -> None:
    """A denial preserves clean provenance even for an untrusted-output tool."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="must not execute")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.DENY),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        output_untrusted=True,
    )
    context = _context(
        db_engine,
        TurnTaintState.empty(),
        turn_id="terminal-denial-result-taint",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "terminal-denial-call",
    )

    assert isinstance(result, ToolResult)
    assert "Action blocked by automatic review" in result.get_text()
    result_taint = context.tool_result_taint_metadata["terminal-denial-call"]
    assert result_taint.get("max_tier") == SourceTrustTier.TRUSTED_USER.config_value
    assert result_taint.get("sources") == []
    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot().sources == ()

    events = await context.db_context.taint_audit_events.list_for_turn(
        "terminal-denial-result-taint"
    )
    assert sum(event["event_type"] == "tool_call_review" for event in events) == 1
    assert all(event["event_type"] != "result_taint" for event in events)


async def test_rejected_review_confirmation_does_not_add_tool_output_taint(
    db_engine: AsyncEngine,
) -> None:
    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="must not execute")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.CONFIRM),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        output_untrusted=True,
    )
    confirmation = _ConfirmationRecorder(ConfirmationOutcome(kind="rejected"))
    context = _context(
        db_engine,
        TurnTaintState.empty(),
        confirmation=confirmation,
        turn_id="rejected-confirmation-result-taint",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "rejected-confirmation-call",
    )

    assert isinstance(result, str)
    assert "cancelled" in result.lower()
    result_taint = context.tool_result_taint_metadata["rejected-confirmation-call"]
    assert result_taint.get("max_tier") == SourceTrustTier.TRUSTED_USER.config_value
    assert result_taint.get("sources") == []
    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot().sources == ()

    events = await context.db_context.taint_audit_events.list_for_turn(
        "rejected-confirmation-result-taint"
    )
    assert sum(event["event_type"] == "tool_call_review" for event in events) == 1
    assert all(event["event_type"] != "result_taint" for event in events)


async def test_terminal_review_denial_preserves_incoming_unknown_taint(
    db_engine: AsyncEngine,
) -> None:
    """A denial keeps incoming taint without inventing a tool-output source."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="must not execute")

    incoming = _unknown_external_state()
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.DENY),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        output_untrusted=True,
    )
    context = _context(
        db_engine,
        incoming,
        turn_id="terminal-denial-incoming-taint",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "terminal-denial-unknown-call",
    )

    assert isinstance(result, ToolResult)
    metadata = context.tool_result_taint_metadata["terminal-denial-unknown-call"]
    assert metadata == incoming.to_metadata()
    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot() == incoming

    events = await context.db_context.taint_audit_events.list_for_turn(
        "terminal-denial-incoming-taint"
    )
    assert sum(event["event_type"] == "tool_call_review" for event in events) == 1
    assert all(event["event_type"] != "result_taint" for event in events)


async def test_failed_denial_escalation_preserves_confirmation_outcome_taint(
    db_engine: AsyncEngine,
) -> None:
    """Confirmation metadata survives without pretending the tool executed."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="must not execute")

    outcome_state = _unknown_external_state()
    confirmation = _ConfirmationRecorder(
        ConfirmationOutcome(
            kind="failed",
            result=ToolResult(text="approved execution failed safely"),
            taint_metadata=outcome_state.to_metadata(),
        )
    )
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.DENY),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        output_untrusted=True,
        review_config=ToolCallReviewConfig(
            timeout_seconds=1,
            escalation=ToolCallReviewEscalationConfig(
                consecutive_denials=1,
                total_denials_per_turn=1,
            ),
        ),
    )
    context = _context(
        db_engine,
        TurnTaintState.empty(),
        confirmation=confirmation,
        turn_id="failed-escalation-result-taint",
    )

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "failed-escalation-call",
    )

    assert isinstance(result, ToolResult)
    assert result.get_text() == "approved execution failed safely"
    assert context.tool_result_taint_metadata["failed-escalation-call"] == (
        outcome_state.to_metadata()
    )
    assert context.taint_tracker is not None
    assert all(
        source.source_type is not TaintSourceType.TOOL_OUTPUT
        for source in context.taint_tracker.snapshot().sources
    )

    events = await context.db_context.taint_audit_events.list_for_turn(
        "failed-escalation-result-taint"
    )
    assert sum(event["event_type"] == "tool_call_review" for event in events) == 1
    assert all(event["event_type"] != "result_taint" for event in events)


async def test_deny_floor_model_verdict_never_escalates_to_human_confirmation(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="must not execute")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    confirmation = _ConfirmationRecorder()
    review_config = ToolCallReviewConfig(
        timeout_seconds=1,
        escalation=ToolCallReviewEscalationConfig(
            consecutive_denials=1,
            total_denials_per_turn=1,
        ),
    )
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            operator_minimum={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.DENY
                }
            },
        ),
        review_config=review_config,
    )
    context = _context(db_engine, _unknown_external_state(), confirmation=confirmation)
    context.tool_call_review_state.consecutive_denials = 10
    context.tool_call_review_state.total_denials = 10

    result = await provider.execute_tool("reviewed_tool", {}, context, "deny-floor")

    assert llm.calls == 1
    assert confirmation.calls == 0
    assert executions == 0
    assert isinstance(result, ToolResult)
    assert "Action blocked by automatic review" in result.get_text()
    assert context.tool_call_review_state.consecutive_denials == 10
    assert context.tool_call_review_state.total_denials == 10
    events = await _review_events(context)
    assert events[0]["review_status"] == ToolCallReviewStatus.MODEL_VERDICT.value


async def test_durable_authorization_does_not_bypass_current_deny_floor(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="must not execute")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            operator_minimum={
                SourceTrustTier.UNKNOWN_EXTERNAL: {
                    SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.DENY
                }
            },
        ),
    )
    context = _context(db_engine, _unknown_external_state())
    authorization = ToolCallReviewAuthorization(
        tool_name="reviewed_tool",
        call_id="deny-floor-approved-call",
        tool_args={},
        sink_class=SinkClass.ARBITRARY_EXTERNAL_MESSAGE.value,
        static_policy_reason=None,
        taint_policy_reason="A previous adjudication escalated to human approval.",
    )
    context.tool_call_review_authorization = authorization

    result = await provider.execute_tool(
        "reviewed_tool",
        {},
        context,
        "deny-floor-approved-call",
    )

    assert isinstance(result, ToolResult)
    assert "Action blocked by automatic review" in result.get_text()
    assert executions == 0
    assert llm.calls == 1
    assert authorization.consumed is False


@pytest.mark.parametrize(
    ("fallback_kind", "expected_status"),
    [
        ("disabled", ToolCallReviewStatus.DISABLED_FALLBACK),
        ("timeout", ToolCallReviewStatus.TIMEOUT_FALLBACK),
        ("budget", ToolCallReviewStatus.BUDGET_FALLBACK),
    ],
)
async def test_deny_fallback_never_escalates_to_human_confirmation(
    db_engine: AsyncEngine,
    fallback_kind: Literal["disabled", "timeout", "budget"],
    expected_status: ToolCallReviewStatus,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="must not execute")

    llm = (
        None
        if fallback_kind == "disabled"
        else _ReviewLLM(
            ToolCallReviewVerdict.DENY,
            raise_timeout=fallback_kind == "timeout",
        )
    )
    confirmation = _ConfirmationRecorder()
    review_config = ToolCallReviewConfig(
        timeout_seconds=1,
        max_reviews_per_turn=1,
        escalation=ToolCallReviewEscalationConfig(
            consecutive_denials=1,
            total_denials_per_turn=1,
        ),
    )
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        sandbox=True,
        review_config=review_config,
    )
    context = _context(db_engine, _unknown_external_state(), confirmation=confirmation)
    context.tool_call_review_state.consecutive_denials = 10
    context.tool_call_review_state.total_denials = 10
    if fallback_kind == "budget":
        context.tool_call_review_state.review_count = 1

    result = await provider.execute_tool(
        "reviewed_tool", {}, context, f"{fallback_kind}-fallback"
    )

    assert confirmation.calls == 0
    assert executions == 0
    assert isinstance(result, ToolResult)
    assert "Action blocked by automatic review" in result.get_text()
    assert context.tool_call_review_state.consecutive_denials == 10
    assert context.tool_call_review_state.total_denials == 10
    events = await _review_events(context)
    assert events[0]["review_status"] == expected_status.value


@pytest.mark.parametrize(
    "escalation",
    [
        ToolCallReviewEscalationConfig(
            consecutive_denials=2,
            total_denials_per_turn=20,
        ),
        ToolCallReviewEscalationConfig(
            consecutive_denials=20,
            total_denials_per_turn=2,
        ),
    ],
    ids=["consecutive-threshold", "total-threshold"],
)
async def test_genuine_model_denial_escalates_only_at_configured_threshold(
    db_engine: AsyncEngine,
    escalation: ToolCallReviewEscalationConfig,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed after human approval")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    confirmation = _ConfirmationRecorder()
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        review_config=ToolCallReviewConfig(
            timeout_seconds=1,
            escalation=escalation,
        ),
        delegation=True,
    )
    context = _context(db_engine, TurnTaintState.empty(), confirmation=confirmation)

    first = await provider.execute_tool("reviewed_tool", {}, context, "first-denial")
    assert isinstance(first, ToolResult)
    assert "Action blocked by automatic review" in first.get_text()
    assert confirmation.calls == 0
    assert executions == 0
    assert context.tool_call_review_state.consecutive_denials == 1
    assert context.tool_call_review_state.total_denials == 1

    second = await provider.execute_tool("reviewed_tool", {}, context, "second-denial")

    assert isinstance(second, ToolResult)
    assert second.get_text() == "executed after human approval"
    assert llm.calls == 2
    assert confirmation.calls == 1
    assert executions == 1
    assert context.tool_call_review_state.consecutive_denials == 0
    assert context.tool_call_review_state.total_denials == 0
    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot().approved_sinks == frozenset()
    assert context.turn_id is not None
    events = await context.db_context.taint_audit_events.list_for_turn(context.turn_id)
    escalation_events = [
        event
        for event in events
        if event["event_type"] == "tool_call_review_escalation"
    ]
    assert len(escalation_events) == 1
    assert escalation_events[0]["review_status"] == "escalation_confirmation_requested"
    assert escalation_events[0]["review_verdict"] == "deny"


async def test_deny_fallback_does_not_break_model_denial_escalation_streak(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed after human approval")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    confirmation = _ConfirmationRecorder()
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        sandbox=True,
        review_config=ToolCallReviewConfig(
            timeout_seconds=1,
            escalation=ToolCallReviewEscalationConfig(
                consecutive_denials=3,
                total_denials_per_turn=20,
            ),
        ),
    )
    context = _context(db_engine, _unknown_external_state(), confirmation=confirmation)

    for call_id in ("first-denial", "second-denial"):
        result = await provider.execute_tool("reviewed_tool", {}, context, call_id)
        assert isinstance(result, ToolResult)
        assert "Action blocked by automatic review" in result.get_text()

    assert context.tool_call_review_state.consecutive_denials == 2
    assert context.tool_call_review_state.total_denials == 2

    llm.raise_timeout = True
    fallback = await provider.execute_tool(
        "reviewed_tool", {}, context, "timeout-fallback"
    )
    assert isinstance(fallback, ToolResult)
    assert "Action blocked by automatic review" in fallback.get_text()
    assert context.tool_call_review_state.consecutive_denials == 2
    assert context.tool_call_review_state.total_denials == 2
    assert confirmation.calls == 0

    llm.raise_timeout = False
    escalated = await provider.execute_tool(
        "reviewed_tool", {}, context, "third-model-denial"
    )

    assert isinstance(escalated, ToolResult)
    assert escalated.get_text() == "executed after human approval"
    assert llm.calls == 4
    assert confirmation.calls == 1
    assert executions == 1
    assert context.tool_call_review_state.consecutive_denials == 0
    assert context.tool_call_review_state.total_denials == 0


@pytest.mark.parametrize("confirmation_kind", ["missing", "deferred-ineligible"])
async def test_unattended_model_denial_threshold_requests_turn_termination_once(
    db_engine: AsyncEngine,
    confirmation_kind: str,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="must not execute")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.DENY),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        review_config=ToolCallReviewConfig(
            timeout_seconds=1,
            escalation=ToolCallReviewEscalationConfig(
                consecutive_denials=1,
                total_denials_per_turn=1,
            ),
        ),
    )
    confirmation = (
        build_deferred_confirmation_callback(
            target_user_id="owner-user",
            source_prefix="From an unattended review — approve to run:",
            missing_owner_message=lambda tool_name: f"No owner for {tool_name}",
        )
        if confirmation_kind == "deferred-ineligible"
        else None
    )
    context = _context(
        db_engine,
        TurnTaintState.empty(),
        confirmation=confirmation,
        turn_id=f"unattended-denial-{confirmation_kind}",
    )

    first = await provider.execute_tool("reviewed_tool", {}, context, "first-denial")
    first_termination_message = (
        context.tool_call_review_state.terminal_denial_escalation_message
    )
    second = await provider.execute_tool("reviewed_tool", {}, context, "second-denial")

    assert isinstance(first, ToolResult)
    assert isinstance(second, ToolResult)
    assert "Action blocked by automatic review" in first.get_text()
    assert executions == 0
    assert context.tool_call_review_state.escalation_handled is True
    assert first_termination_message is not None
    assert (
        context.tool_call_review_state.terminal_denial_escalation_message
        == first_termination_message
    )
    events = await context.db_context.taint_audit_events.list_for_turn(
        f"unattended-denial-{confirmation_kind}"
    )
    escalation_events = [
        event
        for event in events
        if event["event_type"] == "tool_call_review_escalation"
    ]
    assert len(escalation_events) == 1
    assert escalation_events[0]["review_status"] == "escalation_turn_terminated"
    assert escalation_events[0]["effective_outcome"] == "deny"


async def test_rejected_escalation_is_not_repeated_in_the_same_turn(
    db_engine: AsyncEngine,
) -> None:
    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="must not execute")

    confirmation = _ConfirmationRecorder(ConfirmationOutcome(kind="rejected"))
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.DENY),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        review_config=ToolCallReviewConfig(
            timeout_seconds=1,
            escalation=ToolCallReviewEscalationConfig(
                consecutive_denials=1,
                total_denials_per_turn=1,
            ),
        ),
    )
    context = _context(db_engine, TurnTaintState.empty(), confirmation=confirmation)

    first = await provider.execute_tool("reviewed_tool", {}, context, "first")
    second = await provider.execute_tool("reviewed_tool", {}, context, "second")

    assert isinstance(first, str)
    assert "cancelled by user" in first
    assert isinstance(second, ToolResult)
    assert "Action blocked by automatic review" in second.get_text()
    assert confirmation.calls == 1
    assert context.tool_call_review_state.escalation_handled is True


async def test_concurrent_denials_reserve_single_escalation_before_await(
    db_engine: AsyncEngine,
) -> None:
    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="must not execute")

    confirmation = _BlockingConfirmationRecorder(ConfirmationOutcome(kind="rejected"))
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=_ReviewLLM(ToolCallReviewVerdict.DENY),
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        review_config=ToolCallReviewConfig(
            timeout_seconds=1,
            escalation=ToolCallReviewEscalationConfig(
                consecutive_denials=1,
                total_denials_per_turn=1,
            ),
        ),
    )
    context = _context(db_engine, TurnTaintState.empty(), confirmation=confirmation)

    first_task = asyncio.create_task(
        provider.execute_tool("reviewed_tool", {}, context, "first-concurrent")
    )
    await asyncio.wait_for(confirmation.entered.wait(), timeout=_HANG_GUARD_SECONDS)
    second = await asyncio.wait_for(
        provider.execute_tool("reviewed_tool", {}, context, "second-concurrent"),
        timeout=_HANG_GUARD_SECONDS,
    )
    confirmation.release.set()
    first = await first_task

    assert isinstance(first, str)
    assert "cancelled by user" in first
    assert isinstance(second, ToolResult)
    assert "Action blocked by automatic review" in second.get_text()
    assert confirmation.calls == 1
    assert context.tool_call_review_state.escalation_handled is True


async def test_carried_sink_approval_skips_named_sink_readjudication(
    db_engine: AsyncEngine,
) -> None:
    """An upstream approval for this sink answers the downstream profile gate."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state().approve_sink(
            SinkClass.SANDBOX_NETWORK, profile_id="coder"
        ),
        turn_id="approved-profile-sink",
    )

    await provider.authorize_taint_sink(
        name="profile:coder",
        sink_class=SinkClass.SANDBOX_NETWORK,
        arguments={"profile_id": "coder"},
        context=context,
        call_id="profile-sink-1",
        taint_policy=taint_policy,
    )

    assert llm.calls == 0
    assert await _review_events(context) == []


async def test_carried_profile_approval_does_not_skip_another_named_sink(
    db_engine: AsyncEngine,
) -> None:
    """A profile handoff approval is not a class-wide sandbox capability."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state().approve_sink(
            SinkClass.SANDBOX_NETWORK, profile_id="coder"
        ),
        turn_id="approval-does-not-cross-profile",
    )

    with pytest.raises(ToolPolicyDeniedError):
        await provider.authorize_taint_sink(
            name="profile:engineer",
            sink_class=SinkClass.SANDBOX_NETWORK,
            arguments={"profile_id": "engineer"},
            context=context,
            call_id="different-profile-sink",
            taint_policy=taint_policy,
        )

    assert llm.calls == 1


async def test_every_named_sink_call_is_reviewed_in_enforce_mode(
    db_engine: AsyncEngine,
) -> None:
    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW)
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="allowed-profile-sink",
    )

    for call_id, arguments in (
        (
            "profile-sink-allow-1",
            {"profile_id": "coder", "request": _nested_sink_arguments()},
        ),
        (
            "profile-sink-allow-2",
            {
                "request": _nested_sink_arguments(reordered=True),
                "profile_id": "coder",
            },
        ),
    ):
        await provider.authorize_taint_sink(
            name="profile:coder",
            sink_class=SinkClass.SANDBOX_NETWORK,
            arguments=arguments,
            context=context,
            call_id=call_id,
            taint_policy=taint_policy,
        )

    assert llm.calls == 2
    assert context.tool_call_review_state.review_count == 2


async def test_every_named_sink_call_is_reviewed_in_observe_mode(
    db_engine: AsyncEngine,
) -> None:
    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW)
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.OBSERVE)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="shadow-keychute-sink",
    )

    await asyncio.gather(
        *(
            provider.authorize_taint_sink(
                name="keychute_http_request",
                sink_class=SinkClass.SANDBOX_NETWORK,
                arguments=arguments,
                context=context,
                call_id=None,
                taint_policy=taint_policy,
            )
            for arguments in (
                _nested_sink_arguments(),
                _nested_sink_arguments(reordered=True),
            )
        )
    )
    await provider.close()

    assert llm.calls == 2
    assert context.tool_call_review_state.review_count == 2
    reviews = await _review_events(context)
    assert len(reviews) == 2


async def test_named_sink_reviewer_allow_cannot_bypass_later_confirm_floor(
    db_engine: AsyncEngine,
) -> None:
    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW)
    manager = _DecisionOnlyConfirmationManager()
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="floored-profile-sink",
    )
    context.confirmation_ui_managers = cast(
        "dict[str, ConfirmationUIManager]",
        {"test": manager},
    )

    await provider.authorize_taint_sink(
        name="profile:coder",
        sink_class=SinkClass.SANDBOX_NETWORK,
        arguments={"profile_id": "coder"},
        context=context,
        call_id="profile-sink-unfloored",
        taint_policy=taint_policy,
    )
    llm.verdict = ToolCallReviewVerdict.CONFIRM
    confirm_floor_policy = TaintPolicyConfig(
        mode=TaintPolicyMode.ENFORCE,
        operator_minimum={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.CONFIRM
            }
        },
    )
    await provider.authorize_taint_sink(
        name="profile:coder",
        sink_class=SinkClass.SANDBOX_NETWORK,
        arguments={"profile_id": "coder"},
        context=context,
        call_id="profile-sink-floored",
        taint_policy=confirm_floor_policy,
    )

    assert llm.calls == 2
    assert manager.calls == 1
    assert context.tool_call_review_state.review_count == 2
    prompt = manager.requests[0]["prompt_text"]
    assert isinstance(prompt, str)
    assert '"profile_id": "coder"' in prompt
    assert "Automatic review reason:" in prompt


async def test_named_sink_confirmation_refuses_payload_that_cannot_be_shown(
    db_engine: AsyncEngine,
) -> None:
    """A decision-only approval must never hide a truncated egress payload."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.CONFIRM)
    manager = _DecisionOnlyConfirmationManager()
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="oversized-named-sink",
    )
    context.confirmation_ui_managers = cast(
        "dict[str, ConfirmationUIManager]",
        {"test": manager},
    )

    with pytest.raises(ToolPolicyDeniedError, match="does not fit"):
        await provider.authorize_taint_sink(
            name="keychute_http_request",
            sink_class=SinkClass.SANDBOX_NETWORK,
            arguments={
                "url": "https://example.test/upload",
                "method": "POST",
                "body": "x" * 4000,
            },
            context=context,
            call_id="oversized-keychute-request",
            taint_policy=taint_policy,
        )

    assert manager.calls == 0


async def test_named_sink_reviewer_confirmation_is_carried_but_deny_floor_wins(
    db_engine: AsyncEngine,
) -> None:
    """An approved reviewer escalation is reused until an absolute floor applies."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.CONFIRM)
    manager = _DecisionOnlyConfirmationManager()
    taint_policy = TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="approved-reviewer-profile-sink",
    )
    context.confirmation_ui_managers = cast(
        "dict[str, ConfirmationUIManager]",
        {"test": manager},
    )

    for call_id in ("profile-sink-review-1", "profile-sink-review-2"):
        await provider.authorize_taint_sink(
            name="profile:coder",
            sink_class=SinkClass.SANDBOX_NETWORK,
            arguments={"profile_id": "coder"},
            context=context,
            call_id=call_id,
            taint_policy=taint_policy,
        )

    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot().is_sink_approved(
        SinkClass.SANDBOX_NETWORK, profile_id="coder"
    )
    assert llm.calls == 1
    assert manager.calls == 1

    deny_floor_policy = TaintPolicyConfig(
        mode=TaintPolicyMode.ENFORCE,
        operator_minimum={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.DENY
            }
        },
    )
    with pytest.raises(ToolPolicyDeniedError):
        await provider.authorize_taint_sink(
            name="profile:coder",
            sink_class=SinkClass.SANDBOX_NETWORK,
            arguments={"profile_id": "coder"},
            context=context,
            call_id="profile-sink-review-deny-floor",
            taint_policy=deny_floor_policy,
        )

    assert llm.calls == 2
    assert manager.calls == 1


async def test_named_sink_hard_confirmation_is_carried_within_turn(
    db_engine: AsyncEngine,
) -> None:
    """A direct matrix confirmation asks once for later calls to the same sink."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    manager = _DecisionOnlyConfirmationManager()
    taint_policy = TaintPolicyConfig(
        mode=TaintPolicyMode.ENFORCE,
        matrix_overrides={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.CONFIRM
            }
        },
    )
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="approved-hard-confirm-profile-sink",
    )
    context.confirmation_ui_managers = cast(
        "dict[str, ConfirmationUIManager]",
        {"test": manager},
    )

    for call_id in ("profile-sink-confirm-1", "profile-sink-confirm-2"):
        await provider.authorize_taint_sink(
            name="profile:coder",
            sink_class=SinkClass.SANDBOX_NETWORK,
            arguments={"profile_id": "coder"},
            context=context,
            call_id=call_id,
            taint_policy=taint_policy,
        )

    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot().is_sink_approved(
        SinkClass.SANDBOX_NETWORK, profile_id="coder"
    )
    assert llm.calls == 0
    assert manager.calls == 1


async def test_carried_sink_approval_cannot_override_deny_verdict_floor(
    db_engine: AsyncEngine,
) -> None:
    """A deny floor remains absolute even when the same sink was approved."""

    async def execute(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unused")

    llm = _ReviewLLM(ToolCallReviewVerdict.ALLOW)
    taint_policy = TaintPolicyConfig(
        mode=TaintPolicyMode.ENFORCE,
        operator_minimum={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.DENY
            }
        },
    )
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=taint_policy,
    )
    context = _context(
        db_engine,
        _unknown_external_state().approve_sink(
            SinkClass.SANDBOX_NETWORK, profile_id="coder"
        ),
        turn_id="deny-floor-profile-sink",
    )

    with pytest.raises(ToolPolicyDeniedError):
        await provider.authorize_taint_sink(
            name="profile:coder",
            sink_class=SinkClass.SANDBOX_NETWORK,
            arguments={"profile_id": "coder"},
            context=context,
            call_id="profile-sink-2",
            taint_policy=taint_policy,
        )

    assert llm.calls == 1
    reviews = await _review_events(context)
    assert len(reviews) == 1
    assert reviews[0]["review_verdict"] == ToolCallReviewVerdict.DENY.value


async def test_uninstrumented_sensitive_read_disables_later_confined_exemption(
    db_engine: AsyncEngine,
) -> None:
    async def read_sensitive(**_kwargs: object) -> ToolResult:
        return ToolResult(text="private note")

    egress_executions = 0

    async def send_external(**_kwargs: object) -> ToolResult:
        nonlocal egress_executions
        egress_executions += 1
        return ToolResult(text="sent")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    provider = _sensitive_read_and_egress_provider(
        cast("ToolImplementation", read_sensitive),
        cast("ToolImplementation", send_external),
        llm,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="sensitive-read-before-egress",
    )
    context.tool_call_review_messages = (
        UserMessage(content="Current confined request"),
        AssistantMessage(content="Current tool proposal"),
    )

    await provider.execute_tool("get_note", {}, context, "sensitive-read-call")
    assert context.taint_tracker is not None
    read_state = context.taint_tracker.snapshot()
    assert len(read_state.sensitive_reads) == 1
    assert read_state.sensitive_reads[0].scope.kind == "tool"
    assert read_state.sensitive_reads[0].scope.qualifier == "tool:get_note"
    context.taint_policy_snapshot = read_state

    result = await provider.execute_tool("send_external", {}, context, "egress-call")

    assert llm.calls == 1
    assert egress_executions == 0
    assert isinstance(result, ToolResult)
    assert "Action blocked by automatic review" in result.get_text()


async def test_explicit_sensitive_read_scope_avoids_generic_duplicate(
    db_engine: AsyncEngine,
) -> None:
    async def read_sensitive(exec_context: ToolExecutionContext) -> ToolResult:
        record_sensitive_read(
            exec_context,
            kind="notes",
            qualifier="note:private-note",
            surfaced_ids=["private-note"],
        )
        return ToolResult(text="private note")

    provider = _provider(
        cast("ToolImplementation", read_sensitive),
        tool_name="get_note",
        reviewer_llm=None,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        sensitive_read=True,
    )
    context = _context(db_engine, TurnTaintState.empty())

    await provider.execute_tool("get_note", {}, context, "explicit-read")

    assert context.taint_tracker is not None
    reads = context.taint_tracker.snapshot().sensitive_reads
    assert len(reads) == 1
    assert reads[0].scope.kind == "notes"
    assert reads[0].scope.surfaced_ids == frozenset({"private-note"})


async def test_failed_uninstrumented_sensitive_read_does_not_record(
    db_engine: AsyncEngine,
) -> None:
    async def unused_read(**_kwargs: object) -> ToolResult:
        return ToolResult(text="unreachable")

    class RaisingLocalToolsProvider(LocalToolsProvider):
        async def execute_tool(
            self,
            name: str,
            arguments: dict[str, object],
            context: ToolExecutionContext,
            call_id: str | None = None,
        ) -> str | ToolResult:
            del name, arguments, context, call_id
            raise RuntimeError("read failed")

    provider = TaintTrackingToolsProvider(
        RaisingLocalToolsProvider(
            registrations=[
                _registration(
                    cast("ToolImplementation", unused_read),
                    tool_name="get_note",
                    sensitive_read=True,
                )
            ]
        ),
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    context = _context(db_engine, TurnTaintState.empty())

    with pytest.raises(RuntimeError, match="read failed"):
        await provider.execute_tool("get_note", {}, context, "failed-read")

    assert context.taint_tracker is not None
    assert context.taint_tracker.snapshot().sensitive_reads == ()


@pytest.mark.parametrize(
    "variant",
    [
        "exempt",
        "missing_window",
        "prior_history",
        "system_trigger_history",
        "aggregated_context",
        "sensitive_read",
        "history",
        "floor",
    ],
)
async def test_confined_exemption_requires_every_safety_condition(
    db_engine: AsyncEngine,
    variant: Literal[
        "exempt",
        "missing_window",
        "prior_history",
        "system_trigger_history",
        "aggregated_context",
        "sensitive_read",
        "history",
        "floor",
    ],
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="executed under exemption")

    state = _unknown_external_state()
    if variant == "sensitive_read":
        state = state.add_sensitive_read(
            SensitiveReadScope(
                kind="notes",
                qualifier="all notes",
                surfaced_ids=frozenset({"note-1"}),
            ),
            query_origin="model_generated",
        )
    elif variant == "history":
        state = replace(state, history_high_taint_present=True)

    operator_minimum = (
        {
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.CONFIRM
            }
        }
        if variant == "floor"
        else {}
    )
    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(
            mode=TaintPolicyMode.ENFORCE,
            operator_minimum=operator_minimum,
        ),
        include_aggregated_context=variant == "aggregated_context",
    )
    context = _context(
        db_engine,
        state,
        turn_id=f"confined-{variant}-turn",
    )
    if variant == "system_trigger_history":
        context.tool_call_review_trigger = TriggerReviewInput(
            trigger_type="delegation_completion",
            active_request_role="system",
            definition="Handle the completed delegation",
            definition_taint_metadata=TurnTaintState.empty().to_metadata(),
            payload_present=True,
        )
        context.tool_call_review_messages = (
            UserMessage(content="Historical user request"),
            AssistantMessage(content="Historical assistant response"),
            SystemMessage(content="Current system-role wake"),
            AssistantMessage(content="Current tool proposal"),
        )
    elif variant != "missing_window":
        context.tool_call_review_messages = (
            (
                UserMessage(content="Earlier request"),
                AssistantMessage(content="Earlier response"),
            )
            if variant == "prior_history"
            else ()
        ) + (
            UserMessage(content="Current confined request"),
            AssistantMessage(content="Current tool proposal"),
        )

    result = await provider.execute_tool("reviewed_tool", {}, context, "confined-call")

    events = await _review_events(context)
    assert len(events) == 1
    if variant == "exempt":
        assert llm.calls == 0
        assert executions == 1
        assert isinstance(result, ToolResult)
        assert result.get_text() == "executed under exemption"
        assert (
            events[0]["review_status"] == ToolCallReviewStatus.CONFINED_EXEMPTION.value
        )
    else:
        assert llm.calls == 1
        assert executions == 0
        assert isinstance(result, ToolResult)
        assert "Action blocked by automatic review" in result.get_text()
        assert events[0]["review_status"] == ToolCallReviewStatus.MODEL_VERDICT.value


async def test_confined_browser_disclosure_can_use_taint_exemption(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="browser action executed under exemption")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        include_aggregated_context=False,
        browser=True,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="confined-browser-turn",
    )
    context.tool_call_review_messages = (
        UserMessage(content="Current confined browser request"),
        AssistantMessage(content="Current browser proposal"),
    )

    result = await provider.execute_tool("reviewed_tool", {}, context, "browser-call")

    assert llm.calls == 0
    assert executions == 1
    assert isinstance(result, ToolResult)
    assert result.get_text() == "browser action executed under exemption"
    events = await _review_events(context)
    assert len(events) == 1
    assert events[0]["review_status"] == ToolCallReviewStatus.CONFINED_EXEMPTION.value


async def test_static_review_still_reviews_confined_browser_action(
    db_engine: AsyncEngine,
) -> None:
    executions = 0

    async def execute(**_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text="unexpected execution")

    llm = _ReviewLLM(ToolCallReviewVerdict.DENY)
    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=llm,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        include_aggregated_context=False,
        browser=True,
    )
    context = _context(
        db_engine,
        _unknown_external_state(),
        turn_id="static-review-confined-browser-turn",
    )
    context.tool_call_review_messages = (
        UserMessage(content="Current confined browser request"),
        AssistantMessage(content="Current browser proposal"),
    )

    result = await provider.execute_tool("reviewed_tool", {}, context, "browser-call")

    assert llm.calls == 1
    assert executions == 0
    assert isinstance(result, ToolResult)
    assert "Action blocked by automatic review" in result.get_text()


# --- What each gate records for a definition the call goes on to write ---------
#
# The cure is written by the gates themselves, so these read what a tool sees on
# its execution context rather than what any automation tool does with it: a
# verdict maps to a disposition in exactly one place, and every write path
# inherits that mapping by forwarding one field.


class _GateOutcomeRecorder:
    """Records the gate outcome each executing call was admitted under."""

    def __init__(self) -> None:
        self.outcomes: list[DefinitionGateOutcome | None] = []

    @property
    def only(self) -> DefinitionGateOutcome:
        assert len(self.outcomes) == 1
        outcome = self.outcomes[0]
        assert outcome is not None
        return outcome


def _recording_provider(
    db_engine: AsyncEngine,
    *,
    state: TurnTaintState,
    reviewer_verdict: ToolCallReviewVerdict | None,
    static_decision: ToolPolicyDecision,
    taint_policy: TaintPolicyConfig,
    confirmation: RequestConfirmationCallback | None = None,
) -> tuple[_GateOutcomeRecorder, TaintTrackingToolsProvider, ToolExecutionContext]:
    recorder = _GateOutcomeRecorder()
    context = _context(db_engine, state, confirmation=confirmation)

    async def execute(**_kwargs: object) -> ToolResult:
        recorder.outcomes.append(context.definition_gate_outcome)
        return ToolResult(text="executed")

    provider = _provider(
        cast("ToolImplementation", execute),
        reviewer_llm=(
            None if reviewer_verdict is None else _ReviewLLM(reviewer_verdict)
        ),
        static_decision=static_decision,
        taint_policy=taint_policy,
    )
    return recorder, provider, context


def _adjudicating_policy(
    mode: TaintPolicyMode,
    *,
    verdict_floor: TaintPolicyOutcome | None = None,
) -> TaintPolicyConfig:
    overrides: dict[SourceTrustTier, dict[SinkClass, TaintPolicyCell]] = {
        SourceTrustTier.UNKNOWN_EXTERNAL: {
            SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.ADJUDICATE
        }
    }
    if verdict_floor is None:
        return TaintPolicyConfig(mode=mode, matrix_overrides=overrides)
    return TaintPolicyConfig(
        mode=mode,
        matrix_overrides=overrides,
        operator_minimum={
            SourceTrustTier.UNKNOWN_EXTERNAL: {
                SinkClass.ARBITRARY_EXTERNAL_MESSAGE: verdict_floor
            }
        },
    )


async def test_an_allow_records_the_taint_cell_that_delegated_it(
    db_engine: AsyncEngine,
) -> None:
    recorder, provider, context = _recording_provider(
        db_engine,
        state=_unknown_external_state(),
        reviewer_verdict=ToolCallReviewVerdict.ALLOW,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=_adjudicating_policy(TaintPolicyMode.ENFORCE),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    outcome = recorder.only
    assert outcome.disposition is CreationDisposition.JUDGE_ALLOWED
    assert outcome.effective_disposition is CreationDisposition.JUDGE_ALLOWED
    assert outcome.gate.layer is GateLayer.TAINT_CELL
    assert outcome.gate.mode == "enforce"
    assert outcome.gate.verdict_id is not None
    assert outcome.gate.reviewer_revision is not None


async def test_a_static_rule_allow_records_the_static_layer(
    db_engine: AsyncEngine,
) -> None:
    """A clean turn reaches no taint cell, so the static rule is the gate."""
    recorder, provider, context = _recording_provider(
        db_engine,
        state=TurnTaintState.empty(),
        reviewer_verdict=ToolCallReviewVerdict.ALLOW,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    assert recorder.only.gate.layer is GateLayer.STATIC_RULE


async def test_an_approved_escalation_records_the_human_not_the_judge(
    db_engine: AsyncEngine,
) -> None:
    """The prompt rendered the whole call and a human approved it."""
    recorder, provider, context = _recording_provider(
        db_engine,
        state=_unknown_external_state(),
        reviewer_verdict=ToolCallReviewVerdict.CONFIRM,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=_adjudicating_policy(TaintPolicyMode.ENFORCE),
        confirmation=_ConfirmationRecorder(),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    outcome = recorder.only
    assert outcome.disposition is CreationDisposition.HUMAN_CONFIRMED
    assert outcome.gate.layer is GateLayer.CONFIRMATION


async def test_an_unapproved_confirmation_never_reaches_a_write(
    db_engine: AsyncEngine,
) -> None:
    """A rejected escalation stops the call, so there is no definition to cure."""
    recorder, provider, context = _recording_provider(
        db_engine,
        state=_unknown_external_state(),
        reviewer_verdict=ToolCallReviewVerdict.CONFIRM,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=_adjudicating_policy(TaintPolicyMode.ENFORCE),
        confirmation=_ConfirmationRecorder(ConfirmationOutcome(kind="rejected")),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    assert recorder.outcomes == []


async def test_a_confirm_floored_cell_records_a_human_backed_cure_only(
    db_engine: AsyncEngine,
) -> None:
    """A floor that excludes allow leaves the judge no way to cure."""
    recorder, provider, context = _recording_provider(
        db_engine,
        state=_unknown_external_state(),
        reviewer_verdict=ToolCallReviewVerdict.ALLOW,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=_adjudicating_policy(
            TaintPolicyMode.ENFORCE, verdict_floor=TaintPolicyOutcome.CONFIRM
        ),
        confirmation=_ConfirmationRecorder(),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    outcome = recorder.only
    assert outcome.disposition is CreationDisposition.HUMAN_CONFIRMED


async def test_a_static_layer_cannot_widen_a_floored_cell_into_a_cure(
    db_engine: AsyncEngine,
) -> None:
    """The merged review under observe omits the floor; the cure does not follow it.

    A co-gating static ``review`` rule hands the reviewer a verdict space that
    still contains ``allow``, because observe must not let a taint floor block
    the call. That ``allow`` is recorded, and it does not cure: ``enforce``
    could never have issued it.
    """
    recorder, provider, context = _recording_provider(
        db_engine,
        state=_unknown_external_state(),
        reviewer_verdict=ToolCallReviewVerdict.ALLOW,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=_adjudicating_policy(
            TaintPolicyMode.OBSERVE, verdict_floor=TaintPolicyOutcome.CONFIRM
        ),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    outcome = recorder.only
    assert outcome.disposition is CreationDisposition.JUDGE_ALLOWED
    assert not outcome.cure_permitted
    assert outcome.effective_disposition is CreationDisposition.JUDGE_ALLOWED_NONBINDING
    assert not outcome.effective_disposition.cures


async def test_a_hard_confirm_approval_records_the_human(
    db_engine: AsyncEngine,
) -> None:
    recorder, provider, context = _recording_provider(
        db_engine,
        state=TurnTaintState.empty(),
        reviewer_verdict=None,
        static_decision=ToolPolicyDecision.CONFIRM,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        confirmation=_ConfirmationRecorder(),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    outcome = recorder.only
    assert outcome.disposition is CreationDisposition.HUMAN_CONFIRMED
    assert outcome.gate.layer is GateLayer.CONFIRMATION


async def test_a_call_no_gate_examined_records_nothing(
    db_engine: AsyncEngine,
) -> None:
    """No gate, no decision: the write stays on its authoring stamp alone."""
    _recorder, provider, context = _recording_provider(
        db_engine,
        state=TurnTaintState.empty(),
        reviewer_verdict=None,
        static_decision=ToolPolicyDecision.ALLOW,
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    assert _recorder.outcomes == [None]


@pytest.mark.parametrize("mode", [TaintPolicyMode.OBSERVE, TaintPolicyMode.ENFORCE])
async def test_the_outcome_does_not_outlive_the_call_that_earned_it(
    db_engine: AsyncEngine,
    mode: TaintPolicyMode,
) -> None:
    """A later ungated call in the same turn must not inherit an earlier verdict."""
    _recorder, provider, context = _recording_provider(
        db_engine,
        state=TurnTaintState.empty(),
        reviewer_verdict=ToolCallReviewVerdict.ALLOW,
        static_decision=ToolPolicyDecision.REVIEW,
        taint_policy=TaintPolicyConfig(mode=mode),
    )

    await provider.execute_tool("reviewed_tool", {}, context, "call-1")

    assert context.definition_gate_outcome is None
