import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
import telegramify_markdown  # type: ignore[import-untyped]  # No type stubs available
from assertpy import assert_that, soft_assertions
from telegram import Update

# Import mock LLM helpers
from family_assistant.config_models import AppConfig
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.services.confirmation_service import (
    CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
)
from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
)
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.task_worker import TaskWorker, handle_confirmation_tool_execution
from family_assistant.telegram.ui import (
    PendingTelegramConfirmation,
    TelegramConfirmationUIManager,
)
from family_assistant.tools import PolicyEngine, ToolPolicyConfig, ToolPolicyDecision
from family_assistant.tools.infrastructure import (
    PolicyEnforcingToolsProvider,
    find_provider_by_type,
)
from family_assistant.tools.policy import PolicyEvaluation
from family_assistant.tools.types import ConfirmationOutcome
from tests.functional.telegram.test_telegram_handler import (
    create_context,
    create_mock_update,
)
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,  # Use alias for clarity
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    Rule,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

# Import the test fixture and helper functions
from .conftest import TelegramHandlerTestFixture
from .helpers import (
    assert_bot_sent_message_with_keyboard,
    extract_callback_data_from_keyboard,
    wait_for_bot_response,
)

logger = logging.getLogger(__name__)

# --- Constants for Test ---
USER_CHAT_ID = 123
USER_ID = 12345
# Use add_or_update_note, but configure it dynamically in tests
TOOL_NAME_SENSITIVE = "add_or_update_note"


class RecordingConfirmationService:
    """Fake confirmation service that records durable resolution calls."""

    def __init__(self) -> None:
        self.created_request_id = f"confirm_{uuid.uuid4().hex[:12]}"
        self.status = "pending"
        self.approve_calls: list[tuple[str, str, str]] = []
        self.reject_calls: list[tuple[str, str, str]] = []
        self.expire_calls = 0

    async def create_request(
        self,
        *,
        target_user_id: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - fake service records arbitrary tool arguments
        tool_args: dict[str, Any],
        tool_call_id: str | None,
        source_message_internal_id: int | None,
        confirmation_prompt: str,
        expires_at: datetime,
    ) -> dict[str, object]:
        _ = (
            target_user_id,
            tool_name,
            tool_args,
            tool_call_id,
            source_message_internal_id,
            confirmation_prompt,
            expires_at,
        )
        return {"id": self.created_request_id}

    async def approve_and_enqueue_execution(
        self,
        *,
        request_id: str,
        approving_user_id: str,
        approving_interface: str,
    ) -> None:
        self.status = "approved"
        self.approve_calls.append((request_id, approving_user_id, approving_interface))

    async def reject(
        self,
        *,
        request_id: str,
        rejecting_user_id: str,
        rejecting_interface: str,
    ) -> None:
        self.status = "rejected"
        self.reject_calls.append((request_id, rejecting_user_id, rejecting_interface))

    async def get_for_user(
        self,
        *,
        request_id: str,
        user_id: str,
    ) -> dict[str, object]:
        _ = request_id, user_id
        return {"status": self.status}

    async def mark_expired(self, *, now: datetime) -> int:
        _ = now
        self.expire_calls += 1
        return self.expire_calls


class RecordingTelegramBot:
    """Small fake Telegram bot for confirmation UI tests."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, object]] = []
        self.edited_texts: list[dict[str, object]] = []
        self.edited_markups: list[dict[str, object]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None,
        reply_markup: object,
    ) -> SimpleNamespace:
        self.sent_messages.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        })
        return SimpleNamespace(message_id=1)

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: object,
    ) -> None:
        self.edited_markups.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup,
        })

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None,
        reply_markup: object = None,
    ) -> None:
        self.edited_texts.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        })


@pytest.mark.asyncio
async def test_non_durable_telegram_confirmation_uses_local_future_with_service() -> (
    None
):
    """Non-durable Telegram confirmations should not be resolved through storage."""
    confirmation_service = RecordingConfirmationService()
    manager = TelegramConfirmationUIManager(
        application=cast("Any", None),
        confirmation_service=cast("Any", confirmation_service),
    )

    approve_future = asyncio.get_running_loop().create_future()
    approve_pending = PendingTelegramConfirmation(
        decision_future=approve_future,
        execution_future=None,
    )
    await manager._approve_confirmation(str(uuid.uuid4()), USER_ID, approve_pending)
    approve_outcome = await approve_future
    assert approve_outcome.kind == "approved"
    assert confirmation_service.approve_calls == []

    reject_future = asyncio.get_running_loop().create_future()
    reject_pending = PendingTelegramConfirmation(
        decision_future=reject_future,
        execution_future=None,
    )
    await manager._reject_confirmation(str(uuid.uuid4()), USER_ID, reject_pending)
    reject_outcome = await reject_future
    assert reject_outcome.kind == "rejected"
    assert confirmation_service.reject_calls == []


@pytest.mark.asyncio
async def test_durable_telegram_confirmation_timeout_stops_after_approval() -> None:
    """Once approved, the confirmation timeout no longer covers tool execution."""
    confirmation_service = RecordingConfirmationService()
    confirmation_waiters = ConfirmationResultWaiterRegistry()
    bot = RecordingTelegramBot()
    manager = TelegramConfirmationUIManager(
        application=cast("Any", SimpleNamespace(bot=bot)),
        confirmation_timeout=0.2,
        confirmation_service=cast("Any", confirmation_service),
        confirmation_result_waiters=confirmation_waiters,
    )

    confirmation_task = asyncio.create_task(
        manager.request_confirmation(
            conversation_id=str(USER_CHAT_ID),
            interface_type="telegram",
            turn_id="turn-id",
            prompt_text="Confirm test action",
            tool_name="record_tool",
            tool_args={"value": "test"},
            timeout=0.2,
            target_user_id=str(USER_ID),
            tool_call_id="call-id",
            source_message_internal_id=1,
        )
    )

    await wait_for_condition(
        lambda: (
            confirmation_service.created_request_id in manager.pending_confirmations
        ),
        timeout=2.0,
        description="durable Telegram confirmation to be pending",
    )
    pending = manager.pending_confirmations[confirmation_service.created_request_id]

    await manager._approve_confirmation(
        confirmation_service.created_request_id,
        USER_ID,
        pending,
    )

    assert confirmation_service.approve_calls == [
        (confirmation_service.created_request_id, str(USER_ID), "telegram")
    ]
    await wait_for_condition(
        lambda: (
            confirmation_service.created_request_id not in manager.pending_confirmations
        ),
        timeout=2.0,
        description="approved Telegram confirmation to leave pending state",
    )
    assert confirmation_service.expire_calls == 0
    assert not confirmation_task.done()

    assert confirmation_waiters.resolve_completed(
        confirmation_service.created_request_id,
        "executed:test",
    )
    outcome = await asyncio.wait_for(confirmation_task, timeout=2.0)

    assert isinstance(outcome, ConfirmationOutcome)
    assert outcome.kind == "completed"
    assert outcome.result == "executed:test"


@pytest.mark.asyncio
async def test_durable_telegram_confirmation_approval_uses_canonical_user_id() -> None:
    """Telegram callback authorization should use the same canonical id as storage."""
    canonical_user_id = "andrew@example.com"
    confirmation_service = RecordingConfirmationService()
    manager = TelegramConfirmationUIManager(
        application=cast("Any", SimpleNamespace(bot=RecordingTelegramBot())),
        confirmation_service=cast("Any", confirmation_service),
        user_identity_resolver=UserIdentityResolver(
            AppConfig.model_validate({
                "users": [
                    {
                        "id": canonical_user_id,
                        "oidc": {"emails": [canonical_user_id]},
                        "telegram": {"user_ids": [USER_ID]},
                    }
                ]
            })
        ),
    )

    await manager._approve_confirmation(
        "confirm_test",
        USER_ID,
        PendingTelegramConfirmation(
            decision_future=asyncio.get_running_loop().create_future(),
            execution_future=None,
        ),
    )

    assert confirmation_service.approve_calls == [
        ("confirm_test", canonical_user_id, "telegram")
    ]


@pytest.mark.asyncio
async def test_existing_durable_confirmation_sends_inline_keyboard() -> None:
    """Cross-interface durable confirmations should use the standard inline UI."""
    bot = RecordingTelegramBot()
    manager = TelegramConfirmationUIManager(
        application=cast("Any", SimpleNamespace(bot=bot)),
    )

    outcome = await manager.send_existing_confirmation_request(
        conversation_id=str(USER_CHAT_ID),
        request_id="confirm_email123",
        prompt_text="Confirm this email-originated action",
    )

    assert outcome.kind == "completed"
    assert len(bot.sent_messages) == 1
    sent = bot.sent_messages[0]
    assert sent["chat_id"] == USER_CHAT_ID
    reply_markup = sent["reply_markup"]
    assert reply_markup is not None
    buttons = cast("Any", reply_markup).inline_keyboard[0]
    assert buttons[0].callback_data == "confirm:confirm_email123:yes"
    assert buttons[1].callback_data == "confirm:confirm_email123:no"


@pytest.mark.asyncio
async def test_existing_durable_confirmation_truncates_long_telegram_prompt() -> None:
    """Long durable confirmation prompts should still produce a sendable message."""
    bot = RecordingTelegramBot()
    manager = TelegramConfirmationUIManager(
        application=cast("Any", SimpleNamespace(bot=bot)),
    )

    outcome = await manager.send_existing_confirmation_request(
        conversation_id=str(USER_CHAT_ID),
        request_id="confirm_long",
        prompt_text="Email-originated action\n\n" + ("x" * 10000),
    )

    assert outcome.kind == "completed"
    sent_text = bot.sent_messages[0]["text"]
    assert isinstance(sent_text, str)
    assert len(sent_text) < 4096
    assert "Confirmation details truncated" in sent_text


@pytest.mark.asyncio
async def test_durable_telegram_external_approval_timeout_waits_for_execution() -> None:
    """An approval from another interface should prevent local timeout expiry."""
    confirmation_service = RecordingConfirmationService()
    confirmation_waiters = ConfirmationResultWaiterRegistry()
    bot = RecordingTelegramBot()
    manager = TelegramConfirmationUIManager(
        application=cast("Any", SimpleNamespace(bot=bot)),
        confirmation_timeout=0.1,
        confirmation_service=cast("Any", confirmation_service),
        confirmation_result_waiters=confirmation_waiters,
    )

    confirmation_task = asyncio.create_task(
        manager.request_confirmation(
            conversation_id=str(USER_CHAT_ID),
            interface_type="telegram",
            turn_id="turn-id",
            prompt_text="Confirm test action",
            tool_name="record_tool",
            tool_args={"value": "test"},
            timeout=0.1,
            target_user_id=str(USER_ID),
            tool_call_id="call-id",
            source_message_internal_id=1,
        )
    )

    await wait_for_condition(
        lambda: (
            confirmation_service.created_request_id in manager.pending_confirmations
        ),
        timeout=2.0,
        description="durable Telegram confirmation to be pending",
    )

    confirmation_service.status = "approved"

    await wait_for_condition(
        lambda: bool(bot.edited_texts),
        timeout=2.0,
        description="externally approved Telegram confirmation message to be edited",
    )
    await wait_for_condition(
        lambda: (
            confirmation_service.created_request_id not in manager.pending_confirmations
        ),
        timeout=2.0,
        description="externally approved Telegram confirmation to leave pending state",
    )
    assert confirmation_service.expire_calls == 0
    assert not confirmation_task.done()

    assert confirmation_waiters.resolve_completed(
        confirmation_service.created_request_id,
        "executed:external",
    )
    outcome = await asyncio.wait_for(confirmation_task, timeout=2.0)

    assert isinstance(outcome, ConfirmationOutcome)
    assert outcome.kind == "completed"
    assert outcome.result == "executed:external"


def _require_confirmation_for_test_tool(
    fix: TelegramHandlerTestFixture,
    tool_name: str,
) -> None:
    processing_service = fix.processing_service
    assert processing_service is not None
    provider = processing_service.tools_provider

    if hasattr(provider, "_tools_requiring_confirmation"):
        provider._tools_requiring_confirmation.add(tool_name)  # type: ignore[attr-defined]
        return

    if hasattr(provider, "_policy_engine") and hasattr(
        provider, "_tool_definitions_by_confirmation"
    ):
        provider_impl = cast("Any", provider)
        enabled_tool_names = sorted(
            processing_service.service_config.tools_config.get_all_tool_names() or []
        )
        provider_impl._policy_engine = PolicyEngine.from_policy_config(
            ToolPolicyConfig.model_validate({
                "default_decision": ToolPolicyDecision.DENY,
                "rules": [
                    {
                        "match": {"names": enabled_tool_names},
                        "decision": ToolPolicyDecision.ALLOW,
                        "priority": 10,
                    },
                    {
                        "match": {"names": [tool_name]},
                        "decision": ToolPolicyDecision.CONFIRM,
                        "priority": 20,
                    },
                ],
            })
        )
        provider_impl._tool_definitions_by_confirmation.clear()
        return

    raise AssertionError(
        f"Unsupported tools provider for confirmation test: {provider!r}"
    )


@pytest.mark.asyncio
async def test_confirmation_accepted(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests the flow where confirmation is requested and accepted by the user.
    The sensitive tool should be executed, and a success message returned.
    """
    # Arrange
    fix = telegram_handler_fixture

    # Temporarily add add_or_update_note to confirm_tools just for this test
    # This is a test-specific override to verify confirmation flow works
    _require_confirmation_for_test_tool(fix, TOOL_NAME_SENSITIVE)
    # Cast mock_llm to the concrete type to access specific attributes like .rules and ._calls
    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    user_message_id = 401
    # Test data for adding a note (confirmed scenario)
    test_note_title = f"Confirmed Note Add {uuid.uuid4()}"
    test_note_content = "This note required confirmation."
    user_text = (
        f"Please add this note: Title={test_note_title}, Content={test_note_content}"
    )
    tool_call_id = f"call_accept_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note for you."
    llm_final_success_text = f"Okay, I have added the note titled '{test_note_title}'."

    # --- Mock LLM Rules ---
    # 1. User asks -> LLM requests sensitive tool
    def request_delete_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return user_text in get_last_message_text(messages)

    request_tool_output = MockLLMOutput(  # Use MockLLMOutput for rule definition
        content=llm_request_tool_text,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name=TOOL_NAME_SENSITIVE,
                    # Arguments for add_or_update_note
                    arguments=json.dumps({
                        "title": test_note_title,
                        "content": test_note_content,
                    }),
                ),
            )
        ],
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)
    # Define expected tool success message based on mock return value
    # The actual tool returns "has been updated successfully" not "added/updated successfully"
    expected_tool_success_result = (
        f"Note '{test_note_title}' has been updated successfully."
    )

    # 2. Tool result received -> LLM gives final success message
    def success_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            and msg.content
            == expected_tool_success_result  # Match exact success message
            for msg in messages
        )

    final_success_output = MockLLMOutput(
        content=llm_final_success_text
    )  # Use MockLLMOutput
    rule_final_success: Rule = (success_result_matcher, final_success_output)

    mock_llm_client.rules = [rule_request_tool, rule_final_success]  # Use casted client

    # --- No need to create ConfirmingToolsProvider manually ---
    # The Assistant will create it automatically based on confirm_tools config

    # --- Mock Confirmation Manager ---
    # Simulate user ACCEPTING the confirmation prompt
    # fix.mock_confirmation_manager is the AsyncMock that replaced request_confirmation
    fix.mock_confirmation_manager.return_value = ConfirmationOutcome(kind="approved")

    # --- No need to mock tool execution ---
    # The actual add_or_update_note tool will execute after confirmation

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called because the tool was configured to require it
        fix.mock_confirmation_manager.assert_called_once()
        confirmation_call_kwargs = fix.mock_confirmation_manager.call_args.kwargs
        assert_that(confirmation_call_kwargs.get("tool_name")).is_equal_to(
            TOOL_NAME_SENSITIVE
        )
        assert_that(confirmation_call_kwargs.get("tool_args")).is_equal_to({
            "title": test_note_title,
            "content": test_note_content,
        })

        # 2. LLM was called twice (request tool, process result)
        assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(2)

    # 3. Verify bot response via test server
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)
    assert_that(bot_responses).described_as("Bot responses").is_not_empty()

    # Get the final bot message text
    bot_message = bot_responses[-1]
    bot_message_text = bot_message.get("message", {}).get("text", "")

    expected_final_escaped_text = telegramify_markdown.markdownify(
        llm_final_success_text
    )
    assert_that(bot_message_text).described_as("Final bot message text").is_equal_to(
        expected_final_escaped_text
    )


@pytest.mark.asyncio
async def test_confirmation_rejected(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests the flow where confirmation is requested and rejected by the user.
    The sensitive tool should NOT be executed, and a cancellation message returned.
    """
    # Arrange
    fix = telegram_handler_fixture

    # Temporarily add add_or_update_note to confirm_tools just for this test
    # This is a test-specific override to verify confirmation flow works
    _require_confirmation_for_test_tool(fix, TOOL_NAME_SENSITIVE)
    # Cast mock_llm to the concrete type
    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    user_message_id = 501
    # Test data for adding a note (rejected scenario)
    test_note_title = f"Rejected Note Add {uuid.uuid4()}"
    test_note_content = "This note add was rejected."
    user_text = f"Add note: Title={test_note_title}, Content={test_note_content}"
    tool_call_id = f"call_reject_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note."
    # Message returned by ConfirmingToolsProvider on rejection
    # This needs to match the *actual* string from ConfirmingToolsProvider
    tool_cancel_result_text = (
        f"OK. Action cancelled by user for tool '{TOOL_NAME_SENSITIVE}'."
    )

    # Final message from LLM after seeing the cancellation
    llm_final_cancel_text = "Okay, I have cancelled the request."

    def request_delete_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return user_text in get_last_message_text(messages)

    request_tool_output = MockLLMOutput(  # Use MockLLMOutput for rule definition
        content=llm_request_tool_text,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name=TOOL_NAME_SENSITIVE,
                    # Arguments for add_or_update_note
                    arguments=json.dumps({
                        "title": test_note_title,
                        "content": test_note_content,
                    }),
                ),
            )
        ],
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)

    # 2. LLM rule to handle the cancellation message from the tool provider
    def cancel_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            and msg.content == tool_cancel_result_text  # Match the exact string
            for msg in messages
        )

    final_cancel_output = MockLLMOutput(
        content=llm_final_cancel_text
    )  # Use MockLLMOutput
    rule_final_cancel: Rule = (cancel_result_matcher, final_cancel_output)

    mock_llm_client.rules = [rule_request_tool, rule_final_cancel]  # Use casted client
    # --- No need to create ConfirmingToolsProvider manually ---
    # The Assistant will create it automatically based on confirm_tools config

    # --- Mock Confirmation Manager ---
    # Simulate user REJECTING the confirmation prompt
    fix.mock_confirmation_manager.return_value = ConfirmationOutcome(kind="rejected")

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called
        fix.mock_confirmation_manager.assert_called_once()

    # 2. LLM was called twice (request tool, process cancellation result)
    assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(2)

    # 3. Verify bot response via test server
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)
    assert_that(bot_responses).described_as("Bot responses").is_not_empty()

    # Get the final bot message text
    bot_message = bot_responses[-1]
    bot_message_text = bot_message.get("message", {}).get("text", "")

    expected_cancel_escaped_text = telegramify_markdown.markdownify(
        llm_final_cancel_text
    )
    assert_that(bot_message_text).described_as("Final bot message text").is_equal_to(
        expected_cancel_escaped_text
    )


@pytest.mark.asyncio
async def test_confirmation_timed_out(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests the flow where confirmation is requested but times out.
    The sensitive tool should NOT be executed, and a cancellation/timeout message returned.
    """
    # Arrange
    fix = telegram_handler_fixture

    # Temporarily add add_or_update_note to confirm_tools just for this test
    # This is a test-specific override to verify confirmation flow works
    _require_confirmation_for_test_tool(fix, TOOL_NAME_SENSITIVE)
    # Cast mock_llm to the concrete type
    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    user_message_id = 601
    # Test data for adding a note (timeout scenario)
    test_note_title = f"Timeout Note Add {uuid.uuid4()}"
    test_note_content = "This note add timed out."
    user_text = f"Add note: Title={test_note_title}, Content={test_note_content}"
    tool_call_id = f"call_timeout_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note."
    # Message returned by ConfirmingToolsProvider on timeout (same as rejection)
    # This needs to match the *actual* string from ConfirmingToolsProvider for timeout
    tool_timeout_result_text = f"Action cancelled: Confirmation request for tool '{TOOL_NAME_SENSITIVE}' timed out."  # Final message from LLM after seeing the timeout/cancellation
    llm_final_timeout_text = "Okay, the request timed out and was cancelled."

    def request_delete_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return user_text in get_last_message_text(messages)

    request_tool_output = MockLLMOutput(  # Use MockLLMOutput for rule definition
        content=llm_request_tool_text,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name=TOOL_NAME_SENSITIVE,
                    # Arguments for add_or_update_note
                    arguments=json.dumps({
                        "title": test_note_title,
                        "content": test_note_content,
                    }),
                ),
            )
        ],
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)

    # 2. LLM rule to handle the timeout/cancellation message from the tool provider
    def timeout_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            and msg.content == tool_timeout_result_text  # Match the exact string
            for msg in messages
        )

    final_timeout_output = MockLLMOutput(
        content=llm_final_timeout_text
    )  # Use MockLLMOutput
    rule_final_timeout: Rule = (timeout_result_matcher, final_timeout_output)

    mock_llm_client.rules = [rule_request_tool, rule_final_timeout]  # Use casted client
    # --- No need to create ConfirmingToolsProvider manually ---
    # The Assistant will create it automatically based on confirm_tools config

    # --- Mock Confirmation Manager ---
    # Simulate TIMEOUT during the confirmation request
    fix.mock_confirmation_manager.side_effect = asyncio.TimeoutError

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called (and raised TimeoutError)
        fix.mock_confirmation_manager.assert_called_once()

    # 2. LLM was called twice (request tool, process timeout/cancellation result)
    assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(2)

    # 3. Verify bot response via test server
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)
    assert_that(bot_responses).described_as("Bot responses").is_not_empty()

    # Get the final bot message text
    bot_message = bot_responses[-1]
    bot_message_text = bot_message.get("message", {}).get("text", "")

    expected_timeout_escaped_text = telegramify_markdown.markdownify(
        llm_final_timeout_text
    )
    assert_that(bot_message_text).described_as("Final bot message text").is_equal_to(
        expected_timeout_escaped_text
    )


@pytest.mark.asyncio
async def test_confirmation_via_inline_keyboard_does_not_deadlock(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """Inline keyboard confirmation must not deadlock the update pipeline.

    Regression test: python-telegram-bot defaults to sequential update
    processing (concurrent_updates=False). When a tool requires confirmation
    the message handler awaits a future that can only be resolved by the
    callback-query handler. With sequential processing the callback query is
    queued behind the message handler, causing a deadlock.

    This test exercises the full Application.process_update() pipeline
    (unlike other tests that call handlers directly) so the
    concurrent_updates setting is actually in effect.
    """
    fix = telegram_handler_fixture

    # --- 1. Restore the REAL confirmation manager (fixture mocks it) ---
    real_mgr = fix.assistant.telegram_service.confirmation_manager  # type: ignore[union-attr]
    real_method = type(real_mgr).request_confirmation
    # Re-bind on both references that conftest patched
    real_mgr.request_confirmation = real_method.__get__(real_mgr, type(real_mgr))
    fix.handler.confirmation_manager.request_confirmation = real_method.__get__(
        fix.handler.confirmation_manager, type(fix.handler.confirmation_manager)
    )

    # --- 2. Make add_or_update_note require confirmation via policy ---
    policy_provider = find_provider_by_type(
        fix.tools_provider, PolicyEnforcingToolsProvider
    )
    assert policy_provider is not None
    original_eval = policy_provider._policy_engine.evaluate_for_execution

    def _patched_eval(
        descriptor: Any,  # noqa: ANN401
        # ast-grep-ignore: no-dict-any - policy engine signature uses object values
        arguments: dict[str, Any] | None = None,
        can_confirm: bool = True,
    ) -> PolicyEvaluation:
        if descriptor.name == TOOL_NAME_SENSITIVE and can_confirm:
            return PolicyEvaluation(
                decision=ToolPolicyDecision.CONFIRM,
                reason="test: requires confirmation",
            )
        return original_eval(descriptor, arguments=arguments, can_confirm=can_confirm)

    policy_provider._policy_engine.evaluate_for_execution = _patched_eval  # type: ignore[assignment]

    # --- 3. Mock LLM: first call returns a tool call, second returns text ---
    tool_call_id = f"call_keyboard_{uuid.uuid4()}"
    cast("RuleBasedMockLLMClient", fix.mock_llm).rules = [
        (
            lambda kwargs: (
                not any(msg.role == "tool" for msg in kwargs.get("messages", []))
            ),
            MockLLMOutput(
                content="I'll add that note.",
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name=TOOL_NAME_SENSITIVE,
                            arguments=json.dumps({
                                "title": "Deadlock Test",
                                "content": "content",
                            }),
                        ),
                    )
                ],
            ),
        ),
        (
            lambda kwargs: any(
                msg.role == "tool" for msg in kwargs.get("messages", [])
            ),
            MockLLMOutput(content="Note added."),
        ),
    ]

    # --- 4. Initialise Application for process_update() ---
    app = fix.application
    await app.initialize()
    assert fix.assistant.database_engine is not None
    assert fix.assistant.embedding_generator is not None
    assert fix.assistant.telegram_service is not None
    assert fix.assistant.confirmation_result_waiters is not None
    assert fix.assistant.fastapi_app is not None
    shutdown_event = asyncio.Event()
    wake_event = asyncio.Event()
    worker = TaskWorker(
        processing_service=fix.processing_service,
        chat_interface=fix.assistant.telegram_service.chat_interface,
        calendar_config={},
        timezone=fix.processing_service.service_config.timezone,
        embedding_generator=fix.assistant.embedding_generator,
        shutdown_event_instance=shutdown_event,
        engine=fix.assistant.database_engine,
        chat_interfaces=fix.assistant.fastapi_app.state.chat_interfaces,
        handler_timeout=10.0,
        confirmation_result_waiters=fix.assistant.confirmation_result_waiters,
    )
    worker.register_task_handler(
        CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
        handle_confirmation_tool_execution,
    )
    worker_task = asyncio.create_task(worker.run(wake_event))

    try:
        # --- 5. Send user message and push through Application pipeline ---
        send_result = await fix.telegram_client.send_message("Add a note please")
        user_update = Update.de_json(send_result.get("result", {}), fix.bot)
        assert user_update is not None

        # This task will block on the confirmation future
        message_task = asyncio.create_task(app.process_update(user_update))

        # --- 6. Wait for the confirmation inline keyboard to arrive ---
        keyboard_msg = await assert_bot_sent_message_with_keyboard(
            fix.telegram_client, timeout=15.0
        )
        confirm_data = extract_callback_data_from_keyboard(keyboard_msg, button_index=0)
        msg_id = keyboard_msg.get("message", {}).get("message_id")

        # --- 7. Simulate user pressing "Confirm" ---
        cb_result = await fix.telegram_client.send_callback(
            callback_data=confirm_data, message_id=msg_id
        )
        cb_update = Update.de_json(cb_result.get("result", {}), fix.bot)
        assert cb_update is not None

        # With concurrent_updates=True the callback handler runs immediately;
        # with False this would deadlock.
        callback_task = asyncio.create_task(app.process_update(cb_update))
        await wait_for_condition(
            lambda: callback_task.done(),
            timeout=10.0,
            description="Telegram callback task to finish",
        )
        callback_task.result()
        wake_event.set()

        # --- 8. The waiting message task must complete after worker execution ---
        await wait_for_condition(
            lambda: message_task.done(),
            timeout=30.0,
            description="Telegram message task to finish after confirmation",
        )
        message_task.result()

        # --- 9. Verify final response was sent ---
        all_updates = await wait_for_bot_response(
            fix.telegram_client, timeout=5.0, min_messages=2
        )
        texts = [u.get("message", {}).get("text", "") for u in all_updates]
        assert any("added" in t.lower() or "note" in t.lower() for t in texts), (
            f"Expected final response after confirmation, got: {texts}"
        )

    finally:
        policy_provider._policy_engine.evaluate_for_execution = original_eval  # type: ignore[assignment]
        shutdown_event.set()
        wake_event.set()
        with contextlib.suppress(TimeoutError):
            await wait_for_condition(
                lambda: worker_task.done(),
                timeout=2.0,
                description="Telegram confirmation worker task to stop",
            )
        if not worker_task.done():
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        await app.shutdown()
