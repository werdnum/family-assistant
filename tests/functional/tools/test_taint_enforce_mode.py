"""Functional end-to-end tests for runtime taint policy enforce mode.

Exercises scenarios S1 through S7 under taint_policy.mode: enforce:
- S1: Adjudicate cell -> confirm verdict gates execution, creates confirmation request with taint_policy_reason, audits model_verdict.
- S2: Adjudicate cell -> deny verdict blocks tool and audits model_verdict.
- S3: Consecutive denial escalation triggers forced confirmation / turn abort, and resets on allow.
- S4: Reviewer timeout and provider error fall back to confirm (never allow).
- S5: Ambient-write gate under enforce: deny refuses note and leaves nothing saved; timeout falls back to confirmation.
- S6: Observe-mode control: tools execute without confirmation prompts, shadow reviews audit would-be outcomes.
- S7: Verification of static_policy_reason gap on confirmation_requests rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from pydantic import BaseModel

from family_assistant.config_models import (
    ToolCallReviewConfig,
    ToolCallReviewEscalationConfig,
)
from family_assistant.security.ambient_admission import AMBIENT_ADMISSION_EVENT_TYPE
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyMode,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.confirmation_service import ConfirmationService
from family_assistant.services.tool_call_review import (
    ToolCallReviewer,
    ToolCallReviewResponse,
    ToolCallReviewStatus,
    ToolCallReviewVerdict,
)
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.tools import (
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    PolicyRule,
    TaintTrackingToolsProvider,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.metadata import (
    ToolImplementation,
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.notes import add_or_update_note_tool
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolArguments,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)
from tests.functional.notes.ambient_helpers import (
    notes_provider,
    stored_tier,
    tool_context,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.telegram.protocols import ConfirmationUIManager


class ScriptedReviewLLM:
    """Mock LLM returning scripted ToolCallReviewResponses or raising exceptions."""

    def __init__(
        self,
        *responses: ToolCallReviewVerdict | ToolCallReviewResponse | Exception,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        del max_retries
        assert response_model is ToolCallReviewResponse
        idx = len(self.calls)
        self.calls.append({"messages": messages, "response_model": response_model})
        if idx < len(self.responses):
            item = self.responses[idx]
        elif self.responses:
            item = self.responses[-1]
        else:
            item = ToolCallReviewVerdict.ALLOW

        if isinstance(item, Exception):
            raise item
        if isinstance(item, ToolCallReviewVerdict):
            return cast(
                "T",
                ToolCallReviewResponse(
                    verdict=item, reason=f"Scripted review {item.value}."
                ),
            )
        return cast("T", item)


class DurableConfirmationManager:
    """Records durable confirmation requests to the database and returns configured outcome."""

    def __init__(
        self,
        db: Database,
        user_id: str = "test-user",
        kind: Literal["approved", "rejected", "completed"] = "rejected",
    ) -> None:
        self.db = db
        self.user_id = user_id
        self.kind = kind
        self.confirmation_service = ConfirmationService(db=db)
        self.calls: list[dict[str, Any]] = []
        self.prompts: list[str] = []

    async def request_confirmation(self, **kwargs: object) -> ConfirmationOutcome:
        """UI manager protocol for named sink confirmations (ambient admission)."""
        prompt_text = str(kwargs.get("prompt_text", ""))
        self.prompts.append(prompt_text)
        return ConfirmationOutcome(
            kind=cast("Literal['approved', 'rejected', 'completed']", self.kind)
        )

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
        """Callback protocol for tool confirmation requests."""
        request = await self.confirmation_service.create_request(
            target_user_id=self.user_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=call_id,
            source_message_internal_id=None,
            confirmation_prompt=f"Confirm: {context.tool_call_review_confirmation_reason or tool_name}",
            expires_at=datetime.now(UTC) + timedelta(seconds=timeout_seconds),
            decision_only=True,
            processing_profile_id=context.processing_profile_id,
            origin_interface_type=interface_type,
            origin_conversation_id=conversation_id,
            taint_state_json=(
                context.taint_tracker.snapshot().to_metadata()
                if context.taint_tracker is not None
                else None
            ),
            tool_call_review_authorization=context.tool_call_review_authorization,
        )
        self.calls.append({
            "request_id": request["id"],
            "tool_name": tool_name,
            "tool_args": tool_args,
            "call_id": call_id,
            "request": request,
        })
        if self.kind == "approved":
            return ConfirmationOutcome(kind="approved")
        elif self.kind == "rejected":
            return ConfirmationOutcome(
                kind="rejected",
                result=f"Action rejected by user for tool {tool_name}",
            )
        else:
            return ConfirmationOutcome(
                kind="completed",
                result=f"Pending confirmation {request['id']} created.",
                action_attempted=False,
            )


def _make_tool(
    name: str,
    sink_tag: ToolTag,
    execute_fn: ToolImplementation,
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
                    "properties": {"command": {"type": "string"}},
                },
            },
        },
    )
    return ToolRegistration(
        definition=definition,
        implementation=execute_fn,
        metadata=make_local_tool_metadata((sink_tag, ToolTag.OUTPUT_UNSPECIFIED)),
    )


def _make_provider(
    registrations: Sequence[ToolRegistration],
    reviewer_llm: ScriptedReviewLLM | None,
    mode: TaintPolicyMode = TaintPolicyMode.ENFORCE,
    escalation_config: ToolCallReviewEscalationConfig | None = None,
    rules: Sequence[PolicyRule] | None = None,
) -> TaintTrackingToolsProvider:
    local = LocalToolsProvider(registrations=registrations)
    policy_engine = PolicyEngine.from_policy_config(
        ToolPolicyConfig(
            default_decision=ToolPolicyDecision.ALLOW,
            rules=list(rules or []),
        )
    )
    policy = PolicyEnforcingToolsProvider(local, policy_engine)
    review_config = ToolCallReviewConfig(
        timeout_seconds=5.0,
        escalation=escalation_config
        or ToolCallReviewEscalationConfig(consecutive_denials=3),
    )
    reviewer = (
        ToolCallReviewer(cast("LLMInterface", reviewer_llm), review_config)
        if reviewer_llm is not None
        else None
    )
    return TaintTrackingToolsProvider(
        policy,
        taint_policy=TaintPolicyConfig(mode=mode),
        tool_call_reviewer=reviewer,
        review_config=review_config,
        include_aggregated_context=False,
    )


def _unknown_external_tracker(
    source_id: str = "ext-source",
) -> InMemoryTurnTaintTracker:
    state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id=source_id,
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({"source_unknown_external"}),
            reason="Untrusted external email content.",
        )
    )
    return InMemoryTurnTaintTracker(state)


def _exec_context(
    db: Database,
    tracker: InMemoryTurnTaintTracker,
    provider: TaintTrackingToolsProvider,
    turn_id: str = "test-enforce-turn",
    confirmation_manager: DurableConfirmationManager | None = None,
) -> ToolExecutionContext:
    context = tool_context(db, tracker)
    context.turn_id = turn_id
    context.tools_provider = provider
    context.tool_call_review_messages = []
    if confirmation_manager is not None:
        context.request_confirmation_callback = confirmation_manager
        context.confirmation_ui_managers = {
            "web": cast("ConfirmationUIManager", confirmation_manager)
        }
    return context


# =========================================================================== #
# S1: Adjudicate cell -> Reviewer verdict CONFIRM
# =========================================================================== #


@pytest.mark.asyncio
async def test_s1_adjudicate_confirm_gates_execution_and_persists_reason(
    db_engine: AsyncEngine,
) -> None:
    """S1: Adjudicate cell + confirm verdict:

    - The tool is NOT executed before approval.
    - A confirmation_requests row is created.
    - Its taint_policy_reason is non-NULL (carries policy cell reason).
    - Audit records mode=enforce, requested_outcome=adjudicate, effective_outcome=confirm,
      review_status=model_verdict.
    - Sighted human approval subsequently allows the tool to run.
    """
    db = Database(db_engine)
    executed = False

    async def execute_remote(command: str) -> ToolResult:
        nonlocal executed
        executed = True
        return ToolResult(text=f"executed: {command}")

    tool = _make_tool(
        "remote_sandbox",
        ToolTag.CODE_EXECUTION,
        cast("ToolImplementation", execute_remote),
    )
    llm = ScriptedReviewLLM(ToolCallReviewVerdict.CONFIRM)
    provider = _make_provider([tool], llm, mode=TaintPolicyMode.ENFORCE)

    # 1. User has not approved (rejected outcome) -> tool must NOT execute
    confirmation_mgr = DurableConfirmationManager(db, kind="rejected")
    tracker = _unknown_external_tracker()
    context = _exec_context(
        db,
        tracker,
        provider,
        turn_id="s1-turn-unapproved",
        confirmation_manager=confirmation_mgr,
    )

    result = await provider.execute_tool(
        "remote_sandbox",
        {"command": "echo test"},
        context,
        "call-s1-1",
    )

    assert executed is False, "Tool must not execute before approval"
    result_text = result.get_text() if isinstance(result, ToolResult) else str(result)
    assert "Action cancelled by user" in result_text

    # Check confirmation_requests row in DB
    pending = await db.confirmation_requests.list_pending_for_user("test-user")
    assert len(pending) == 1
    req = pending[0]
    assert req["tool_name"] == "remote_sandbox"
    assert req["sink_class"] == SinkClass.SANDBOX_NETWORK.value
    assert req["taint_policy_reason"] is not None
    assert (
        "adjudicates" in req["taint_policy_reason"]
        or "sandbox_network" in req["taint_policy_reason"]
    )

    # Check audit events
    events = await db.taint_audit_events.list_for_turn("s1-turn-unapproved")
    policy_eval = next(e for e in events if e["event_type"] == "policy_evaluation")
    review_eval = next(e for e in events if e["event_type"] == "tool_call_review")

    assert policy_eval["mode"] == "enforce"
    assert policy_eval["requested_outcome"] == "adjudicate"
    assert policy_eval["effective_outcome"] == "adjudicate"

    assert review_eval["mode"] == "enforce"
    assert review_eval["review_status"] == ToolCallReviewStatus.MODEL_VERDICT.value
    assert review_eval["review_verdict"] == "confirm"
    assert review_eval["effective_outcome"] == "confirm"

    # 2. Sighted human approval branch -> tool executes
    llm_approved = ScriptedReviewLLM(ToolCallReviewVerdict.CONFIRM)
    provider_approved = _make_provider(
        [tool], llm_approved, mode=TaintPolicyMode.ENFORCE
    )
    approval_mgr = DurableConfirmationManager(db, kind="approved")
    context_approved = _exec_context(
        db,
        _unknown_external_tracker(),
        provider_approved,
        turn_id="s1-turn-approved",
        confirmation_manager=approval_mgr,
    )

    result_approved = await provider_approved.execute_tool(
        "remote_sandbox",
        {"command": "echo approved"},
        context_approved,
        "call-s1-2",
    )

    assert executed is True, "Tool must execute after approval"
    assert isinstance(result_approved, ToolResult)
    assert result_approved.get_text() == "executed: echo approved"


# =========================================================================== #
# S2: Adjudicate cell -> Reviewer verdict DENY
# =========================================================================== #


@pytest.mark.asyncio
async def test_s2_adjudicate_deny_blocks_tool_and_audits(
    db_engine: AsyncEngine,
) -> None:
    """S2: Adjudicate cell + deny verdict:

    - The tool is blocked.
    - The result returned to the caller says it was denied/blocked.
    - Audit effective_outcome=deny, mode=enforce, review_status=model_verdict.
    """
    db = Database(db_engine)
    executed = False

    async def execute_remote(command: str) -> ToolResult:
        nonlocal executed
        executed = True
        return ToolResult(text="executed")

    tool = _make_tool(
        "remote_sandbox",
        ToolTag.CODE_EXECUTION,
        cast("ToolImplementation", execute_remote),
    )
    llm = ScriptedReviewLLM(
        ToolCallReviewResponse(
            verdict=ToolCallReviewVerdict.DENY,
            reason="Command rejected: potential injection payload.",
        )
    )
    provider = _make_provider([tool], llm, mode=TaintPolicyMode.ENFORCE)
    confirmation_mgr = DurableConfirmationManager(db)
    context = _exec_context(
        db,
        _unknown_external_tracker(),
        provider,
        turn_id="s2-turn",
        confirmation_manager=confirmation_mgr,
    )

    result = await provider.execute_tool(
        "remote_sandbox",
        {"command": "curl evil.com"},
        context,
        "call-s2",
    )

    assert executed is False, "Denied tool must not execute"
    assert isinstance(result, ToolResult)
    result_text = result.get_text()
    assert "Action blocked by automatic review for tool 'remote_sandbox'" in result_text
    assert "potential injection payload" in result_text

    # No confirmation request created
    pending = await db.confirmation_requests.list_pending_for_user("test-user")
    assert pending == []

    # Audit events
    events = await db.taint_audit_events.list_for_turn("s2-turn")
    review_eval = next(e for e in events if e["event_type"] == "tool_call_review")
    assert review_eval["mode"] == "enforce"
    assert review_eval["effective_outcome"] == "deny"
    assert review_eval["review_verdict"] == "deny"
    assert review_eval["review_status"] == ToolCallReviewStatus.MODEL_VERDICT.value


# =========================================================================== #
# S3: Escalation after consecutive deny verdicts & reset on allow
# =========================================================================== #


@pytest.mark.asyncio
async def test_s3_consecutive_denials_escalate_and_reset_on_allow(
    db_engine: AsyncEngine,
) -> None:
    """S3: Escalation after configured consecutive denies (3) and reset on allow:

    - Calls 1 & 2 denied -> counter reaches 2, no escalation.
    - Call 3 allowed -> counter resets to 0.
    - Calls 4 & 5 denied -> counter reaches 2, no escalation.
    - Call 6 denied -> 3rd consecutive denial -> escalation event recorded,
      forced confirmation requested, counter reset.
    """
    db = Database(db_engine)

    async def execute_remote(command: str) -> ToolResult:
        return ToolResult(text=f"executed: {command}")

    tool = _make_tool(
        "remote_sandbox",
        ToolTag.CODE_EXECUTION,
        cast("ToolImplementation", execute_remote),
    )

    # Deny, Deny, Allow, Deny, Deny, Deny
    llm = ScriptedReviewLLM(
        ToolCallReviewVerdict.DENY,
        ToolCallReviewVerdict.DENY,
        ToolCallReviewVerdict.ALLOW,
        ToolCallReviewVerdict.DENY,
        ToolCallReviewVerdict.DENY,
        ToolCallReviewVerdict.DENY,
    )
    provider = _make_provider(
        [tool],
        llm,
        mode=TaintPolicyMode.ENFORCE,
        escalation_config=ToolCallReviewEscalationConfig(consecutive_denials=3),
    )
    confirmation_mgr = DurableConfirmationManager(db, kind="rejected")
    context = _exec_context(
        db,
        _unknown_external_tracker(),
        provider,
        turn_id="s3-turn",
        confirmation_manager=confirmation_mgr,
    )

    # Call 1: Deny
    await provider.execute_tool("remote_sandbox", {"command": "1"}, context, "c1")
    assert context.tool_call_review_state.consecutive_denials == 1
    assert not context.tool_call_review_state.escalation_handled

    # Call 2: Deny
    await provider.execute_tool("remote_sandbox", {"command": "2"}, context, "c2")
    assert context.tool_call_review_state.consecutive_denials == 2
    assert not context.tool_call_review_state.escalation_handled

    # Call 3: Allow -> resets consecutive denials
    res3 = await provider.execute_tool(
        "remote_sandbox", {"command": "3"}, context, "c3"
    )
    assert isinstance(res3, ToolResult)
    assert res3.get_text() == "executed: 3"
    assert context.tool_call_review_state.consecutive_denials == 0
    assert not context.tool_call_review_state.escalation_handled

    # Call 4: Deny
    await provider.execute_tool("remote_sandbox", {"command": "4"}, context, "c4")
    assert context.tool_call_review_state.consecutive_denials == 1

    # Call 5: Deny
    await provider.execute_tool("remote_sandbox", {"command": "5"}, context, "c5")
    assert context.tool_call_review_state.consecutive_denials == 2
    assert not context.tool_call_review_state.escalation_handled

    # Call 6: Deny -> 3rd consecutive denial triggers escalation!
    await provider.execute_tool("remote_sandbox", {"command": "6"}, context, "c6")
    assert context.tool_call_review_state.escalation_handled is True
    # Counters reset after escalation handling
    assert context.tool_call_review_state.consecutive_denials == 0

    # Escalation audit event was recorded
    events = await db.taint_audit_events.list_for_turn("s3-turn")
    escalation_events = [
        e for e in events if e["event_type"] == "tool_call_review_escalation"
    ]
    assert len(escalation_events) == 1
    escalation = escalation_events[0]
    assert escalation["mode"] == "enforce"
    assert escalation["review_status"] == "escalation_confirmation_requested"

    # Forced confirmation was requested
    assert len(confirmation_mgr.calls) == 1
    escalation_req = confirmation_mgr.calls[0]["request"]
    assert escalation_req["tool_name"] == "remote_sandbox"
    assert "repeatedly denied" in escalation_req["confirmation_prompt"]


@pytest.mark.asyncio
async def test_s3_escalation_turn_terminated_when_confirmation_unavailable(
    db_engine: AsyncEngine,
) -> None:
    """S3 variant: When confirmation channel is unavailable, repeated denials terminate turn."""
    db = Database(db_engine)

    async def execute_remote(command: str) -> ToolResult:
        return ToolResult(text=f"executed: {command}")

    tool = _make_tool(
        "remote_sandbox",
        ToolTag.CODE_EXECUTION,
        cast("ToolImplementation", execute_remote),
    )
    llm = ScriptedReviewLLM(
        ToolCallReviewVerdict.DENY,
        ToolCallReviewVerdict.DENY,
        ToolCallReviewVerdict.DENY,
    )
    provider = _make_provider(
        [tool],
        llm,
        mode=TaintPolicyMode.ENFORCE,
        escalation_config=ToolCallReviewEscalationConfig(consecutive_denials=3),
    )
    # No confirmation callback registered (unattended context)
    context = _exec_context(
        db,
        _unknown_external_tracker(),
        provider,
        turn_id="s3-turn-no-confirm",
        confirmation_manager=None,
    )

    await provider.execute_tool("remote_sandbox", {"command": "1"}, context, "c1")
    await provider.execute_tool("remote_sandbox", {"command": "2"}, context, "c2")
    await provider.execute_tool("remote_sandbox", {"command": "3"}, context, "c3")

    assert context.tool_call_review_state.escalation_handled is True
    assert context.tool_call_review_state.terminal_denial_escalation_message is not None
    assert (
        "stopped this turn after automatic review repeatedly denied"
        in context.tool_call_review_state.terminal_denial_escalation_message
    )

    events = await db.taint_audit_events.list_for_turn("s3-turn-no-confirm")
    escalation_events = [
        e for e in events if e["event_type"] == "tool_call_review_escalation"
    ]
    assert len(escalation_events) == 1
    assert escalation_events[0]["review_status"] == "escalation_turn_terminated"


# =========================================================================== #
# S4: Reviewer timeout & error -> confirm fallback
# =========================================================================== #


@pytest.mark.asyncio
async def test_s4_reviewer_timeout_falls_back_to_confirm(
    db_engine: AsyncEngine,
) -> None:
    """S4: Reviewer timeout in enforce mode:

    - Fallback is confirm (never allow).
    - Tool does not execute before approval.
    - Confirmation request row is created with taint_policy_reason.
    - Audit records review_status=timeout_fallback, effective_outcome=confirm, mode=enforce.
    """
    db = Database(db_engine)
    executed = False

    async def execute_remote(command: str) -> ToolResult:
        nonlocal executed
        executed = True
        return ToolResult(text="executed")

    tool = _make_tool(
        "remote_sandbox",
        ToolTag.CODE_EXECUTION,
        cast("ToolImplementation", execute_remote),
    )
    llm = ScriptedReviewLLM(TimeoutError("Reviewer request timed out"))
    provider = _make_provider([tool], llm, mode=TaintPolicyMode.ENFORCE)
    confirmation_mgr = DurableConfirmationManager(db, kind="rejected")
    context = _exec_context(
        db,
        _unknown_external_tracker(),
        provider,
        turn_id="s4-timeout-turn",
        confirmation_manager=confirmation_mgr,
    )

    result = await provider.execute_tool(
        "remote_sandbox",
        {"command": "echo test"},
        context,
        "call-s4-timeout",
    )

    assert executed is False, "Tool must not execute on timeout fallback"
    result_text = result.get_text() if isinstance(result, ToolResult) else str(result)
    assert "Action cancelled by user" in result_text

    pending = await db.confirmation_requests.list_pending_for_user("test-user")
    assert len(pending) == 1
    req = pending[0]
    assert req["taint_policy_reason"] is not None

    events = await db.taint_audit_events.list_for_turn("s4-timeout-turn")
    review_eval = next(e for e in events if e["event_type"] == "tool_call_review")
    assert review_eval["mode"] == "enforce"
    assert review_eval["review_status"] == ToolCallReviewStatus.TIMEOUT_FALLBACK.value
    assert review_eval["effective_outcome"] == "confirm"
    assert review_eval["review_verdict"] == "confirm"


@pytest.mark.asyncio
async def test_s4_reviewer_error_falls_back_to_confirm(
    db_engine: AsyncEngine,
) -> None:
    """S4: Reviewer provider error in enforce mode:

    - Fallback is confirm.
    - Audit records review_status=provider_error_fallback, effective_outcome=confirm.
    - Confirmation request row is created with taint_policy_reason.
    """
    db = Database(db_engine)
    executed = False

    async def execute_remote(command: str) -> ToolResult:
        nonlocal executed
        executed = True
        return ToolResult(text="executed")

    tool = _make_tool(
        "remote_sandbox",
        ToolTag.CODE_EXECUTION,
        cast("ToolImplementation", execute_remote),
    )
    llm = ScriptedReviewLLM(RuntimeError("Remote provider 500 internal server error"))
    provider = _make_provider([tool], llm, mode=TaintPolicyMode.ENFORCE)
    confirmation_mgr = DurableConfirmationManager(db, kind="rejected")
    context = _exec_context(
        db,
        _unknown_external_tracker(),
        provider,
        turn_id="s4-error-turn",
        confirmation_manager=confirmation_mgr,
    )

    result = await provider.execute_tool(
        "remote_sandbox",
        {"command": "echo test"},
        context,
        "call-s4-error",
    )

    assert executed is False
    result_text = result.get_text() if isinstance(result, ToolResult) else str(result)
    assert "Action cancelled by user" in result_text

    pending = await db.confirmation_requests.list_pending_for_user("test-user")
    assert len(pending) == 1
    req = pending[0]
    assert req["taint_policy_reason"] is not None

    events = await db.taint_audit_events.list_for_turn("s4-error-turn")
    review_eval = next(e for e in events if e["event_type"] == "tool_call_review")
    assert review_eval["mode"] == "enforce"
    assert (
        review_eval["review_status"]
        == ToolCallReviewStatus.PROVIDER_ERROR_FALLBACK.value
    )
    assert review_eval["effective_outcome"] == "confirm"
    assert review_eval["review_verdict"] == "confirm"


# =========================================================================== #
# S5: Ambient-write gate under enforce mode
# =========================================================================== #


@pytest.mark.asyncio
async def test_s5_ambient_write_gate_denial_refuses_and_does_not_save(
    db_engine: AsyncEngine,
) -> None:
    """S5: add_or_update_note with include_in_prompt=True from unknown_external:

    - Reviewer returns deny.
    - Note is NOT saved in db and NOT loaded into prompts.
    - Result explains note was not admitted and nothing was saved.
    - Audit event_type=ambient_note_admission, sink_class=ambient_prompt_write,
      mode=enforce, effective_outcome=refused.
    """
    db = Database(db_engine)
    llm = ScriptedReviewLLM(ToolCallReviewVerdict.DENY)
    provider = _make_provider([], llm, mode=TaintPolicyMode.ENFORCE)
    tracker = _unknown_external_tracker()
    context = _exec_context(db, tracker, provider, turn_id="s5-turn-deny")

    result = await add_or_update_note_tool(
        context,
        title="Shopping Routine",
        content="Buy groceries every Monday.",
        include_in_prompt=True,
    )

    assert result.startswith("Error:")
    assert "Nothing was saved" in result
    assert "It can be saved as a reference note instead" in result

    # Verify not in db
    assert (
        await db.notes.get_by_title(
            "Shopping Routine", read_policy=NoteReadPolicy.UNRESTRICTED
        )
        is None
    )

    # Verify not in prompts
    prompt_text = "\n".join(
        await notes_provider(db).get_context_fragments(acting_user_id=None)
    )
    assert "Buy groceries" not in prompt_text

    # Audit event
    events = await db.taint_audit_events.list_since(
        datetime(2000, 1, 1, tzinfo=UTC), limit=50
    )
    admission_events = [
        e for e in events if e["event_type"] == AMBIENT_ADMISSION_EVENT_TYPE
    ]
    assert len(admission_events) == 1
    event = admission_events[0]
    assert event["sink_class"] == SinkClass.AMBIENT_PROMPT_WRITE.value
    assert event["mode"] == "enforce"
    assert event["effective_outcome"] == "refused"
    assert event["review_verdict"] == "deny"
    assert event["review_status"] == ToolCallReviewStatus.MODEL_VERDICT.value


@pytest.mark.asyncio
async def test_s5_ambient_write_gate_timeout_fallback_to_confirmation(
    db_engine: AsyncEngine,
) -> None:
    """S5: Reviewer times out during ambient note admission:

    - Fallback is confirm.
    - Sighted user approval admits the note into future prompts at MACHINE_REVIEWED tier.
    - Audit records review_status=timeout_fallback, effective_outcome=admitted.
    """
    db = Database(db_engine)
    llm = ScriptedReviewLLM(TimeoutError("Admission reviewer timed out"))
    provider = _make_provider([], llm, mode=TaintPolicyMode.ENFORCE)
    confirmation_mgr = DurableConfirmationManager(db, kind="approved")
    tracker = _unknown_external_tracker()
    context = _exec_context(
        db,
        tracker,
        provider,
        turn_id="s5-turn-timeout",
        confirmation_manager=confirmation_mgr,
    )

    result = await add_or_update_note_tool(
        context,
        title="Packing List",
        content="Passport, tickets, chargers.",
        include_in_prompt=True,
    )

    assert "Packing List" in result
    assert len(confirmation_mgr.prompts) == 1
    assert "Passport, tickets" in confirmation_mgr.prompts[0]

    # Stored note has MACHINE_REVIEWED tier
    assert await stored_tier(db, "Packing List") is SourceTrustTier.MACHINE_REVIEWED

    # Prompt contains admitted note
    prompt_text = "\n".join(
        await notes_provider(db).get_context_fragments(acting_user_id=None)
    )
    assert "Passport, tickets, chargers." in prompt_text

    # Audit event
    events = await db.taint_audit_events.list_since(
        datetime(2000, 1, 1, tzinfo=UTC), limit=50
    )
    admission_events = [
        e
        for e in events
        if e["event_type"] == AMBIENT_ADMISSION_EVENT_TYPE
        and e["artifact_id"] == "note:Packing List"
    ]
    assert len(admission_events) == 1
    event = admission_events[0]
    assert event["sink_class"] == SinkClass.AMBIENT_PROMPT_WRITE.value
    assert event["mode"] == "enforce"
    assert event["effective_outcome"] == "admitted"
    assert event["review_status"] == ToolCallReviewStatus.TIMEOUT_FALLBACK.value


# =========================================================================== #
# S6: Observe-mode control for S1, S2, and S4
# =========================================================================== #


@pytest.mark.asyncio
async def test_s6_observe_mode_control_for_confirm_deny_and_timeout(
    db_engine: AsyncEngine,
) -> None:
    """S6: Observe-mode controls for S1, S2, and S4:

    - With mode=observe, the tool executes directly without confirmation.
    - No confirmation_requests row is created.
    - Shadow review executes in the background and audits mode=observe with the would-be verdict.
    """
    db = Database(db_engine)
    executions = 0

    async def execute_remote(command: str) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(text=f"executed: {command}")

    tool = _make_tool(
        "remote_sandbox",
        ToolTag.CODE_EXECUTION,
        cast("ToolImplementation", execute_remote),
    )

    # 1. Observe control for S1 (CONFIRM verdict)
    llm_confirm = ScriptedReviewLLM(ToolCallReviewVerdict.CONFIRM)
    provider_confirm = _make_provider([tool], llm_confirm, mode=TaintPolicyMode.OBSERVE)
    conf_mgr = DurableConfirmationManager(db)
    ctx_confirm = _exec_context(
        db,
        _unknown_external_tracker(),
        provider_confirm,
        turn_id="s6-confirm-turn",
        confirmation_manager=conf_mgr,
    )

    res_confirm = await provider_confirm.execute_tool(
        "remote_sandbox", {"command": "s1_cmd"}, ctx_confirm, "c-s6-1"
    )
    assert executions == 1
    assert isinstance(res_confirm, ToolResult)
    assert res_confirm.get_text() == "executed: s1_cmd"
    await provider_confirm.close()  # Drain background shadow review

    events_confirm = await db.taint_audit_events.list_for_turn("s6-confirm-turn")
    review_confirm = next(
        e for e in events_confirm if e["event_type"] == "tool_call_review"
    )
    assert review_confirm["mode"] == "observe"
    assert review_confirm["review_verdict"] == "confirm"

    # 2. Observe control for S2 (DENY verdict)
    llm_deny = ScriptedReviewLLM(ToolCallReviewVerdict.DENY)
    provider_deny = _make_provider([tool], llm_deny, mode=TaintPolicyMode.OBSERVE)
    ctx_deny = _exec_context(
        db,
        _unknown_external_tracker(),
        provider_deny,
        turn_id="s6-deny-turn",
        confirmation_manager=conf_mgr,
    )

    res_deny = await provider_deny.execute_tool(
        "remote_sandbox", {"command": "s2_cmd"}, ctx_deny, "c-s6-2"
    )
    assert executions == 2
    assert isinstance(res_deny, ToolResult)
    assert res_deny.get_text() == "executed: s2_cmd"
    await provider_deny.close()

    events_deny = await db.taint_audit_events.list_for_turn("s6-deny-turn")
    review_deny = next(e for e in events_deny if e["event_type"] == "tool_call_review")
    assert review_deny["mode"] == "observe"
    assert review_deny["review_verdict"] == "deny"

    # 3. Observe control for S4 (TIMEOUT fallback)
    llm_timeout = ScriptedReviewLLM(TimeoutError("Shadow review timed out"))
    provider_timeout = _make_provider([tool], llm_timeout, mode=TaintPolicyMode.OBSERVE)
    ctx_timeout = _exec_context(
        db,
        _unknown_external_tracker(),
        provider_timeout,
        turn_id="s6-timeout-turn",
        confirmation_manager=conf_mgr,
    )

    res_timeout = await provider_timeout.execute_tool(
        "remote_sandbox", {"command": "s4_cmd"}, ctx_timeout, "c-s6-3"
    )
    assert executions == 3
    assert isinstance(res_timeout, ToolResult)
    assert res_timeout.get_text() == "executed: s4_cmd"
    await provider_timeout.close()

    events_timeout = await db.taint_audit_events.list_for_turn("s6-timeout-turn")
    review_timeout = next(
        e for e in events_timeout if e["event_type"] == "tool_call_review"
    )
    assert review_timeout["mode"] == "observe"
    assert review_timeout["review_verdict"] == "confirm"
    assert (
        review_timeout["review_status"] == ToolCallReviewStatus.TIMEOUT_FALLBACK.value
    )

    # Across all observe tests, no confirmation request was ever created in DB
    pending = await db.confirmation_requests.list_pending_for_user("test-user")
    assert pending == []


# =========================================================================== #
# S7: Check static_policy_reason on confirmation_requests
# =========================================================================== #


@pytest.mark.asyncio
async def test_s7_static_policy_reason_is_null_on_static_confirmation(
    db_engine: AsyncEngine,
) -> None:
    """S7: Demonstrate that static policy confirmation leaves static_policy_reason NULL.

    When a tool is gated by a static ToolPolicyDecision.CONFIRM rule:
    - The request_taint_confirmation path does not populate ToolCallReviewAuthorization,
      so ConfirmationService receives static_policy_reason=None.
    - Thus, static_policy_reason is NULL in production on every row.
    """
    db = Database(db_engine)

    async def execute_fn(**_kwargs: object) -> ToolResult:
        return ToolResult(text="executed")

    tool = _make_tool(
        "restricted_tool",
        ToolTag.STATE_CHANGING,
        cast("ToolImplementation", execute_fn),
    )
    # Configure static policy rule: restricted_tool -> CONFIRM
    rule = PolicyRule(
        match=ToolMatcher(names=["restricted_tool"]),
        decision=ToolPolicyDecision.CONFIRM,
        description="Static policy requires confirmation for restricted_tool.",
    )
    provider = _make_provider([tool], None, mode=TaintPolicyMode.ENFORCE, rules=[rule])
    conf_mgr = DurableConfirmationManager(db, kind="rejected")
    # Clean turn state (no taint)
    tracker = InMemoryTurnTaintTracker()
    context = _exec_context(
        db,
        tracker,
        provider,
        turn_id="s7-turn",
        confirmation_manager=conf_mgr,
    )

    await provider.execute_tool("restricted_tool", {}, context, "call-s7")

    pending = await db.confirmation_requests.list_pending_for_user("test-user")
    assert len(pending) == 1
    req = pending[0]
    assert req["tool_name"] == "restricted_tool"
    # Document the gap: static_policy_reason is NULL
    assert req["static_policy_reason"] is None
