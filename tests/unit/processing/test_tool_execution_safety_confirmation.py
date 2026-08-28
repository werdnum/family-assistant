"""Tests for Gemini computer-use safety confirmation handling in ToolExecutor."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing.attachments import AttachmentProcessor
from family_assistant.processing.tool_execution import ToolExecutor
from family_assistant.processing.types import ToolExecutionResult
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.deferred_tool_confirmation import (
    DeferredConfirmationCallbackAdapter,
)
from family_assistant.storage.database import Database
from family_assistant.tools.computer_use_names import COMPUTER_USE_FUNCTION_NAMES
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolAttachment,
    ToolResult,
)
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from family_assistant.skills.registry import NoteRegistry
    from family_assistant.tools import ToolExecutionContext


class MinimalToolsProvider:
    """Minimal fake tools provider for testing."""

    def __init__(
        self,
        result: str | ToolResult = "OK",
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.executed_tool_names: list[str] = []
        self.executed_arguments: list[dict] = []

    async def get_tool_definitions(self) -> list:
        return []

    async def execute_tool(
        self,
        name: str,
        arguments: dict,
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        if self.error is not None:
            raise self.error
        self.executed_tool_names.append(name)
        if name in COMPUTER_USE_FUNCTION_NAMES:
            assert "safety_decision" not in arguments
        self.executed_arguments.append(dict(arguments))
        return self.result

    async def close(self) -> None:
        pass


class MinimalToolExecutorConfig:
    """Minimal config satisfying the ToolExecutorConfig protocol."""

    def __init__(self, confirmation_timeout_seconds: float = 3600.0) -> None:
        self.tools_config = ToolsConfig(
            confirmation_timeout_seconds=confirmation_timeout_seconds
        )

    timezone: ZoneInfo = ZoneInfo("UTC")
    id: str = "test"
    visibility_grants: set[str] | None = None
    default_note_visibility_labels: list[str] | None = None
    required_note_visibility_labels: list[str] | None = None
    allowed_note_visibility_labels: list[str] | None = None
    allow_wake_llm: bool = True
    note_registry: NoteRegistry | None = None


def make_tool_executor(
    provider: MinimalToolsProvider | None = None,
    confirmation_timeout_seconds: float = 3600.0,
) -> ToolExecutor:
    """Create a ToolExecutor for testing."""
    # Real AttachmentProcessor with no registry: results in these tests stay
    # far below the large-result threshold, so it is a pass-through.
    attachment_processor = AttachmentProcessor(
        attachment_registry=None,
        llm_client=Mock(),
        app_config=AppConfig(),
        clock=SystemClock(),
    )
    return ToolExecutor(
        tools_provider=provider or MinimalToolsProvider(),
        config=MinimalToolExecutorConfig(confirmation_timeout_seconds),
        attachment_processor=attachment_processor,
        attachment_registry=None,
        clock=SystemClock(),
        credential_resolvers=None,
        api_backend=None,
    )


class StubConfirmationCallback:
    """Stub async confirmation callback for testing."""

    def __init__(self, outcome: ConfirmationOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    async def __call__(
        self,
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: dict,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        self.calls.append({
            "tool_name": tool_name,
            "call_id": call_id,
            "tool_args": tool_args,
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "timeout_seconds": timeout_seconds,
        })
        return self.outcome


@pytest.mark.asyncio
async def test_safety_decision_stripped_before_execution() -> None:
    """Test that safety_decision is always popped from arguments."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="take_screenshot",
            arguments={
                "intent": "look around",
                "safety_decision": {"decision": "allowed", "explanation": "none"},
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=None,
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_arguments) == 1
    assert provider.executed_arguments[0] == {"intent": "look around"}


@pytest.mark.asyncio
async def test_safety_decision_require_confirmation_approved() -> None:
    """Test tool execution when safety confirmation is approved."""
    provider = MinimalToolsProvider(result=ToolResult(data={"status": "done"}))
    executor = make_tool_executor(provider)

    callback = StubConfirmationCallback(outcome=ConfirmationOutcome(kind="approved"))

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 100,
                "y": 200,
                "intent": "click button",
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "Clicking confirm payment button",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_tool_names) == 1
    assert provider.executed_tool_names[0] == "click"

    assert len(callback.calls) == 1
    callback_args = callback.calls[0]
    assert callback_args["tool_name"] == "click"
    assert "safety_decision" in callback_args["tool_args"]
    assert callback_args["tool_args"]["x"] == 100

    response_payload = json.loads(result.llm_message.content)
    assert response_payload["safety_acknowledgement"] is True
    assert response_payload["status"] == "done"


@pytest.mark.asyncio
async def test_safety_decision_require_confirmation_rejected() -> None:
    """Test tool NOT executed when safety confirmation is rejected."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    callback = StubConfirmationCallback(outcome=ConfirmationOutcome(kind="rejected"))

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 100,
                "y": 200,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "Suspicious click",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_tool_names) == 0
    assert "cancelled" in result.llm_message.content.lower()


@pytest.mark.asyncio
async def test_safety_decision_require_confirmation_timed_out() -> None:
    """Test tool NOT executed when confirmation times out."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    callback = StubConfirmationCallback(outcome=ConfirmationOutcome(kind="timed_out"))

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 100,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "Testing timeout",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_tool_names) == 0
    assert "timed out" in result.llm_message.content.lower()


@pytest.mark.asyncio
async def test_safety_decision_no_callback_available() -> None:
    """Test error when safety confirmation needed but no callback."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 100,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "No callback scenario",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=None,
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_tool_names) == 0
    assert "does not support confirmations" in result.llm_message.content


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allowed", "allow", "regular"])
async def test_safety_decision_allowed_no_confirmation(decision: str) -> None:
    """Test tool execution for safety decisions that permit execution."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 100,
                "y": 200,
                "safety_decision": {
                    "decision": decision,
                    "explanation": "Safe action",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=None,
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_tool_names) == 1
    assert provider.executed_tool_names[0] == "click"


@pytest.mark.asyncio
async def test_safety_decision_approved_but_tool_fails() -> None:
    """Test that no acknowledgement is added when tool execution fails."""
    provider = MinimalToolsProvider(error=RuntimeError("Tool failed"))
    executor = make_tool_executor(provider)

    callback = StubConfirmationCallback(outcome=ConfirmationOutcome(kind="approved"))

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 1,
                "y": 2,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "Test failure",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert isinstance(result, ToolExecutionResult)
    # The user approved the gated action, so even a failed attempt carries the
    # acknowledgement — the model needs it to process the error and move on.
    payload = json.loads(result.llm_message.content)
    assert payload["safety_acknowledgement"] is True
    assert "Tool failed" in payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["blocked", "some_future_value", None])
async def test_safety_decision_blocked_or_unknown_refused(
    decision: str | None,
) -> None:
    """Decisions that neither allow nor request confirmation refuse execution.

    A ``None`` decision models a malformed safety_decision dict that omits
    its decision value entirely — it must also fail closed.
    """
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 100,
                "y": 200,
                "safety_decision": {
                    "decision": decision,
                    "explanation": "Prohibited by safety policy",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=None,
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_tool_names) == 0
    assert "not executed" in result.llm_message.content
    assert "Prohibited by safety policy" in result.llm_message.content


class RaisingConfirmationCallback:
    """Confirmation callback that raises the configured exception."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __call__(
        self,
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: dict,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        raise self.error


@pytest.mark.asyncio
async def test_safety_confirmation_timeout_error_yields_declined_result() -> None:
    """A TimeoutError from the callback is an expected not-taken outcome."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 100,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "Testing timeout",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=RaisingConfirmationCallback(TimeoutError()),
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_tool_names) == 0
    assert "timed out" in result.llm_message.content.lower()


@pytest.mark.asyncio
async def test_safety_confirmation_unexpected_error_propagates() -> None:
    """Unexpected callback failures propagate instead of masking the bug."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 100,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "Testing failure",
                },
            },
        ),
    )

    with pytest.raises(RuntimeError, match="confirmation infrastructure broke"):
        await executor.execute(
            tool_call,
            interface_type="test",
            conversation_id="conv_123",
            user_name="testuser",
            turn_id="turn_1",
            db_context=Mock(spec=Database),
            chat_interface=None,
            request_confirmation_callback=RaisingConfirmationCallback(
                RuntimeError("confirmation infrastructure broke")
            ),
        )


@pytest.mark.asyncio
async def test_safety_decision_not_interpreted_outside_action_space() -> None:
    """Non-computer-use tools keep a legitimately named safety_decision arg."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="some_mcp_tool",
            arguments={
                "param1": "value1",
                "safety_decision": {"decision": "blocked"},
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=None,
    )

    assert isinstance(result, ToolExecutionResult)
    assert len(provider.executed_arguments) == 1
    assert provider.executed_arguments[0] == {
        "param1": "value1",
        "safety_decision": {"decision": "blocked"},
    }


@pytest.mark.asyncio
async def test_safety_confirmation_uses_configured_timeout() -> None:
    """The profile's confirmation timeout flows into the safety callback."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider, confirmation_timeout_seconds=120.0)

    callback = StubConfirmationCallback(outcome=ConfirmationOutcome(kind="approved"))

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 1,
                "y": 2,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "check",
                },
            },
        ),
    )

    await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert [call["timeout_seconds"] for call in callback.calls] == [120.0]


@pytest.mark.asyncio
async def test_safety_confirmation_completed_result_preserved_with_ack() -> None:
    """A durable confirmation's completed ToolResult flows through with ack."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    completed_result = ToolResult(
        data={"url": "https://example.com"},
        attachments=[
            ToolAttachment(
                content=b"png-bytes",
                mime_type="image/png",
                description="Browser screenshot",
            )
        ],
    )
    completed_taint = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="call_123",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="browser output",
        )
    )
    callback = StubConfirmationCallback(
        outcome=ConfirmationOutcome(
            kind="completed",
            result=completed_result,
            taint_metadata=completed_taint.to_metadata(),
        )
    )
    turn_taint_tracker = InMemoryTurnTaintTracker()

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 1,
                "y": 2,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "check",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
        taint_tracker=turn_taint_tracker,
    )

    assert isinstance(result, ToolExecutionResult)
    # The tool was NOT executed locally — the durable path already ran it.
    assert provider.executed_tool_names == []
    payload = json.loads(result.llm_message.content)
    assert payload["safety_acknowledgement"] is True
    assert payload["url"] == "https://example.com"
    assert result.llm_message.transient_attachments is not None
    assert result.llm_message.taint_metadata is not None
    assert (
        TurnTaintState.from_metadata(result.llm_message.taint_metadata).max_tier
        == SourceTrustTier.UNKNOWN_EXTERNAL
    )
    assert turn_taint_tracker.snapshot().max_tier == SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_safety_confirmation_oversized_type_text_refused() -> None:
    """A safety-gated type call whose text can't be fully shown is refused."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    callback = StubConfirmationCallback(outcome=ConfirmationOutcome(kind="approved"))

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="type",
            arguments={
                "text": "x" * 2000,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "typing a lot",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert isinstance(result, ToolExecutionResult)
    assert provider.executed_tool_names == []
    assert callback.calls == []
    assert "smaller pieces" in result.llm_message.content


@pytest.mark.asyncio
async def test_safety_confirmation_deferred_pending_not_acknowledged() -> None:
    """A deferred 'completed' placeholder (tool hasn't run) must not claim consent."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    pending_message = (
        "Waiting on the user to approve this in Telegram or the web UI "
        "(request abc123). It hasn't run yet."
    )
    wrapped_callback = StubConfirmationCallback(
        outcome=ConfirmationOutcome(
            kind="completed",
            result=pending_message,
        )
    )
    callback = DeferredConfirmationCallbackAdapter(wrapped_callback)

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 1,
                "y": 2,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "check",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert isinstance(result, ToolExecutionResult)
    assert provider.executed_tool_names == []
    assert len(wrapped_callback.calls) == 1
    assert "hasn't run yet" in result.llm_message.content
    assert "safety_acknowledgement" not in result.llm_message.content


@pytest.mark.asyncio
async def test_safety_confirmation_completed_string_result_acknowledged() -> None:
    """A completed durable string result represents an attempted action."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    callback = StubConfirmationCallback(
        outcome=ConfirmationOutcome(
            kind="completed",
            result="Error executing approved tool 'right_click': unsupported backend",
        )
    )

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="right_click",
            arguments={
                "x": 1,
                "y": 2,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "check",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert isinstance(result, ToolExecutionResult)
    assert provider.executed_tool_names == []
    payload = json.loads(result.llm_message.content)
    assert payload["safety_acknowledgement"] is True
    assert "unsupported backend" in payload["result"]


@pytest.mark.asyncio
async def test_safety_confirmation_failed_durable_execution_acknowledged() -> None:
    """An approved-but-failed durable execution carries the acknowledgement."""
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    failed_taint = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="call_123",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="failed browser output",
        )
    )
    callback = StubConfirmationCallback(
        outcome=ConfirmationOutcome(
            kind="failed",
            result="Error executing approved tool 'click': backend exploded",
            taint_metadata=failed_taint.to_metadata(),
        )
    )
    turn_taint_tracker = InMemoryTurnTaintTracker()

    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(
            name="click",
            arguments={
                "x": 1,
                "y": 2,
                "safety_decision": {
                    "decision": "require_confirmation",
                    "explanation": "check",
                },
            },
        ),
    )

    result = await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
        taint_tracker=turn_taint_tracker,
    )

    assert isinstance(result, ToolExecutionResult)
    assert provider.executed_tool_names == []
    payload = json.loads(result.llm_message.content)
    assert payload["safety_acknowledgement"] is True
    assert "backend exploded" in payload["error"]
    assert result.llm_message.taint_metadata is not None
    assert (
        TurnTaintState.from_metadata(result.llm_message.taint_metadata).max_tier
        == SourceTrustTier.UNKNOWN_EXTERNAL
    )
    assert turn_taint_tracker.snapshot().max_tier == SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_original_tool_call_arguments_not_mutated() -> None:
    """Popping safety_decision must not rewrite the ToolCallItem's arguments.

    The provider hands arguments over as a dict that is the same object stored
    on the assistant message; that message is replayed to the LLM on the next
    iteration and must still carry the safety_decision the model emitted.
    """
    provider = MinimalToolsProvider()
    executor = make_tool_executor(provider)

    callback = StubConfirmationCallback(outcome=ConfirmationOutcome(kind="approved"))

    original_arguments = {
        "x": 100,
        "y": 200,
        "safety_decision": {
            "decision": "require_confirmation",
            "explanation": "check",
        },
    }
    tool_call = ToolCallItem(
        id="call_123",
        type="function",
        function=ToolCallFunction(name="click", arguments=original_arguments),
    )

    await executor.execute(
        tool_call,
        interface_type="test",
        conversation_id="conv_123",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=callback,
    )

    assert tool_call.function.arguments == {
        "x": 100,
        "y": 200,
        "safety_decision": {
            "decision": "require_confirmation",
            "explanation": "check",
        },
    }
