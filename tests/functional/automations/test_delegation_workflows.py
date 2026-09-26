"""Complex delegation workflows and edge cases with attachments."""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import (
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import LLMMessage
from family_assistant.processing import (
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations_map,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition_list,
)
from family_assistant.tools import (
    LocalToolsProvider,
)
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    extract_text_from_content,
    get_last_message_text,
    get_message_content,
    get_message_role,
    last_real_message,
)

logger = logging.getLogger(__name__)

# --- Test Constants ---
PRIMARY_PROFILE_ID = "primary_delegator"
SPECIALIZED_PROFILE_ID = "specialized_target"
DELEGATED_TASK_DESCRIPTION = "Solve this complex problem for me."
USER_QUERY_TEMPLATE = "Please delegate this task: {task_description}"

TEST_CHAT_ID = 123456789
TEST_INTERFACE_TYPE = "test_interface"
TEST_USER_NAME = "DelegationTester"

SPECIALIST_SAW_ATTACHMENT_REPLY = "Specialist analysed the attached image."
SPECIALIST_NO_ATTACHMENT_REPLY = "Specialist received no attachment."


def _user_texts(messages: list[LLMMessage]) -> list[str]:
    return [
        extract_text_from_content(get_message_content(msg))
        for msg in messages
        if get_message_role(msg) == "user"
    ]


def _sees_delegated_attachment(messages: list[LLMMessage], attachment_id: str) -> bool:
    """Whether the delegated request and the attachment's ID marker reached the LLM."""
    texts = _user_texts(messages)
    return any(DELEGATED_TASK_DESCRIPTION in text for text in texts) and any(
        f"[Attachment ID: {attachment_id}]" in text for text in texts
    )


def _is_initial_user_turn(kwargs: MatcherArgs) -> bool:
    last_message = last_real_message(kwargs.get("messages", []))
    return last_message is not None and last_message.role == "user"


def _is_delegation_result(kwargs: MatcherArgs) -> bool:
    last_message = last_real_message(kwargs.get("messages", []))
    return (
        last_message is not None
        and last_message.role == "tool"
        and last_message.name == "delegate_to_service"
    )


def _relay_delegation_result(kwargs: MatcherArgs) -> MockLLMOutput:
    return MockLLMOutput(
        content=f"The specialist replied: {get_last_message_text(kwargs['messages'])}"
    )


def _attachment_seeing_specialist(attachment_id: str) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(
        rules=[
            (
                lambda kwargs: _sees_delegated_attachment(
                    kwargs["messages"], attachment_id
                ),
                MockLLMOutput(content=SPECIALIST_SAW_ATTACHMENT_REPLY),
            )
        ],
        default_response=MockLLMOutput(content=SPECIALIST_NO_ATTACHMENT_REPLY),
    )


def _delegating_primary(attachment_id: str) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(
        rules=[
            (
                _is_initial_user_turn,
                MockLLMOutput(
                    content="I'll delegate this task with the attachment.",
                    tool_calls=[
                        ToolCallItem(
                            id="delegate_call",
                            type="function",
                            function=ToolCallFunction(
                                name="delegate_to_service",
                                arguments=json.dumps({
                                    "target_service_id": SPECIALIZED_PROFILE_ID,
                                    "user_request": DELEGATED_TASK_DESCRIPTION,
                                    "confirm_delegation": False,
                                    "attachment_ids": [attachment_id],
                                }),
                            ),
                        )
                    ],
                ),
            ),
            (_is_delegation_result, _relay_delegation_result),
        ]
    )


def _assert_specialist_saw_attachment_once(
    specialized_llm_client: RuleBasedMockLLMClient, attachment_id: str
) -> None:
    specialized_calls = specialized_llm_client.get_calls()
    assert len(specialized_calls) == 1, (
        f"Expected exactly one delegated LLM call, got {len(specialized_calls)}"
    )
    messages_to_specialized = specialized_calls[0]["kwargs"]["messages"]
    assert _sees_delegated_attachment(messages_to_specialized, attachment_id), (
        f"Attachment {attachment_id} was not injected into the delegated request. "
        f"User messages were: {_user_texts(messages_to_specialized)}"
    )


@pytest.mark.asyncio
async def test_delegate_to_service_with_attachments(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
    tmp_path: Path,
) -> None:
    """Test delegating requests with attachments."""
    logger.info("--- Test: Delegation With Attachments ---")

    # Create attachment registry
    test_storage = tmp_path / "test_attachments"
    test_storage.mkdir(exist_ok=True)
    attachment_registry = AttachmentRegistry(
        storage_path=str(test_storage), db_engine=db_engine, config=None
    )

    # Create a test attachment
    test_content = b"Test image content for delegation"
    db_context = Database(engine=db_engine)
    attachment_record = await attachment_registry.register_user_attachment(
        db_context=db_context,
        content=test_content,
        mime_type="image/png",
        filename="test_image.png",
        conversation_id=str(TEST_CHAT_ID),
        user_id=TEST_USER_NAME,
        description="Test image for delegation",
    )
    test_attachment_id = attachment_record.attachment_id

    specialized_llm_client = _attachment_seeing_specialist(test_attachment_id)
    primary_llm_client = _delegating_primary(test_attachment_id)

    # Create services
    primary_tools_provider = LocalToolsProvider(
        definitions=local_tools_definition_list,
        implementations=local_tool_implementations_map,
    )

    primary_service = ProcessingService(
        llm_client=primary_llm_client,
        tools_provider=primary_tools_provider,
        service_config=ProcessingServiceConfig(
            id=PRIMARY_PROFILE_ID,
            prompts={"system_prompt": "I am a primary assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        attachment_registry=attachment_registry,
    )

    specialized_service = ProcessingService(
        llm_client=specialized_llm_client,
        tools_provider=LocalToolsProvider(definitions=[], implementations={}),
        service_config=ProcessingServiceConfig(
            id=SPECIALIZED_PROFILE_ID,
            prompts={"system_prompt": "I am a specialized assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        attachment_registry=attachment_registry,
    )

    # Set up registry
    registry = {
        PRIMARY_PROFILE_ID: primary_service,
        SPECIALIZED_PROFILE_ID: specialized_service,
    }
    primary_service.processing_services_registry = registry
    specialized_service.processing_services_registry = registry
    task_worker_manager(
        primary_service,
        MagicMock(spec=ChatInterface),
        register_delegation_handler=True,
    )

    # Execute delegation with attachments
    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    db_context = Database(engine=db_engine)
    result = await primary_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=str(TEST_CHAT_ID),
        trigger_content_parts=[
            {"type": "text", "text": user_query},
            {"type": "attachment", "attachment_id": test_attachment_id},
        ],
        trigger_interface_message_id="msg_attach",
        user_name=TEST_USER_NAME,
        chat_interface=MagicMock(spec=ChatInterface),
        request_confirmation_callback=None,
    )

    assert result.error_traceback is None, (
        f"Error during attachment delegation: {result.error_traceback}"
    )
    assert SPECIALIST_SAW_ATTACHMENT_REPLY in (result.text_reply or ""), (
        f"Delegated reply did not reach the primary: {result.text_reply}"
    )
    _assert_specialist_saw_attachment_once(specialized_llm_client, test_attachment_id)


@pytest.mark.asyncio
async def test_delegate_to_service_cross_conversation_attachment_allowed(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
    tmp_path: Path,
) -> None:
    """Test that delegation succeeds even when using attachments from different conversations."""
    logger.info("--- Test: Delegation Cross-Conversation Attachment Allowed ---")

    # Create attachment registry
    test_storage = tmp_path / "test_attachments"
    test_storage.mkdir(exist_ok=True)
    attachment_registry = AttachmentRegistry(
        storage_path=str(test_storage), db_engine=db_engine, config=None
    )

    # Create a test attachment in a different conversation
    other_conversation_id = "other_conversation_123"
    test_content = b"Test image content from other conversation"
    db_context = Database(engine=db_engine)
    attachment_record = await attachment_registry.register_user_attachment(
        db_context=db_context,
        content=test_content,
        mime_type="image/png",
        filename="other_test_image.png",
        conversation_id=other_conversation_id,  # Different conversation
        user_id=TEST_USER_NAME,
        description="Test image from other conversation",
    )
    other_attachment_id = attachment_record.attachment_id

    primary_llm_client = _delegating_primary(other_attachment_id)
    specialized_llm_client = _attachment_seeing_specialist(other_attachment_id)

    # Create services
    primary_tools_provider = LocalToolsProvider(
        definitions=local_tools_definition_list,
        implementations=local_tool_implementations_map,
    )

    primary_service = ProcessingService(
        llm_client=primary_llm_client,
        tools_provider=primary_tools_provider,
        service_config=ProcessingServiceConfig(
            id=PRIMARY_PROFILE_ID,
            prompts={"system_prompt": "I am a primary assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        attachment_registry=attachment_registry,
    )

    specialized_service = ProcessingService(
        llm_client=specialized_llm_client,
        tools_provider=LocalToolsProvider(definitions=[], implementations={}),
        service_config=ProcessingServiceConfig(
            id=SPECIALIZED_PROFILE_ID,
            prompts={"system_prompt": "I am a specialized assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        attachment_registry=attachment_registry,
    )

    # Set up registry
    registry = {
        PRIMARY_PROFILE_ID: primary_service,
        SPECIALIZED_PROFILE_ID: specialized_service,
    }
    primary_service.processing_services_registry = registry
    specialized_service.processing_services_registry = registry
    task_worker_manager(
        primary_service,
        MagicMock(spec=ChatInterface),
        register_delegation_handler=True,
    )

    # Execute delegation
    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    db_context = Database(engine=db_engine)
    result = await primary_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=str(TEST_CHAT_ID),
        trigger_content_parts=[{"type": "text", "text": user_query}],
        trigger_interface_message_id="msg_security_test",
        user_name=TEST_USER_NAME,
        chat_interface=MagicMock(spec=ChatInterface),
        request_confirmation_callback=None,
    )

    assert result.error_traceback is None, (
        f"Error during cross-conversation delegation: {result.error_traceback}"
    )
    assert SPECIALIST_SAW_ATTACHMENT_REPLY in (result.text_reply or ""), (
        f"Delegated reply did not reach the primary: {result.text_reply}"
    )
    _assert_specialist_saw_attachment_once(specialized_llm_client, other_attachment_id)


@pytest.mark.asyncio
async def test_delegate_to_service_propagates_generated_attachments(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
    tmp_path: Path,
) -> None:
    """Test that attachments generated by the delegated service are propagated back to primary profile."""
    logger.info("--- Test: Delegation Propagates Generated Attachments ---")

    # Create attachment registry
    test_storage = tmp_path / "test_attachments"
    test_storage.mkdir(exist_ok=True)
    attachment_registry = AttachmentRegistry(
        storage_path=str(test_storage), db_engine=db_engine, config=None
    )

    # Create LLM client for delegated service that will use a tool to generate an attachment
    def delegated_service_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last_message = last_real_message(messages)
        return (
            last_message is not None
            and last_message.role == "user"
            and DELEGATED_TASK_DESCRIPTION in (last_message.content or "")
        )

    # The delegated service will call mock_camera_snapshot which returns a ToolResult with attachment
    delegated_llm_client = RuleBasedMockLLMClient(
        rules=[
            (
                delegated_service_matcher,
                MockLLMOutput(
                    content="I'll capture a camera snapshot for you.",
                    tool_calls=[
                        ToolCallItem(
                            id="camera_call",
                            type="function",
                            function=ToolCallFunction(
                                name="mock_camera_snapshot",
                                arguments=json.dumps({"entity_id": "camera.test"}),
                            ),
                        )
                    ],
                ),
            ),
            # After tool executes, LLM provides final response
            (
                lambda kwargs: any(
                    msg.role == "tool" for msg in kwargs.get("messages", [])
                ),
                MockLLMOutput(
                    content="Here's the camera snapshot I captured for your request.",
                    tool_calls=None,
                ),
            ),
        ]
    )

    # Create primary LLM client that delegates
    def primary_delegation_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last_message = last_real_message(messages)
        return (
            last_message is not None
            and last_message.role == "user"
            and "delegate" in get_last_message_text(messages).lower()
        )

    primary_llm_client = RuleBasedMockLLMClient(
        rules=[
            (
                primary_delegation_matcher,
                MockLLMOutput(
                    content="I'll delegate this to the specialized service.",
                    tool_calls=[
                        ToolCallItem(
                            id="delegate_call",
                            type="function",
                            function=ToolCallFunction(
                                name="delegate_to_service",
                                arguments=json.dumps({
                                    "target_service_id": SPECIALIZED_PROFILE_ID,
                                    "user_request": DELEGATED_TASK_DESCRIPTION,
                                    "confirm_delegation": False,
                                }),
                            ),
                        )
                    ],
                ),
            ),
            (_is_delegation_result, _relay_delegation_result),
        ]
    )

    # Create services with tool access
    primary_tools_provider = LocalToolsProvider(
        definitions=local_tools_definition_list,
        implementations=local_tool_implementations_map,
    )

    specialized_tools_provider = LocalToolsProvider(
        definitions=local_tools_definition_list,
        implementations=local_tool_implementations_map,
    )

    primary_service = ProcessingService(
        llm_client=primary_llm_client,
        tools_provider=primary_tools_provider,
        service_config=ProcessingServiceConfig(
            id=PRIMARY_PROFILE_ID,
            prompts={"system_prompt": "I am a primary assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        attachment_registry=attachment_registry,
    )

    specialized_service = ProcessingService(
        llm_client=delegated_llm_client,
        tools_provider=specialized_tools_provider,
        service_config=ProcessingServiceConfig(
            id=SPECIALIZED_PROFILE_ID,
            prompts={
                "system_prompt": "I am a specialized assistant with camera access."
            },
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        attachment_registry=attachment_registry,
    )

    # Set up registry
    registry = {
        PRIMARY_PROFILE_ID: primary_service,
        SPECIALIZED_PROFILE_ID: specialized_service,
    }
    primary_service.processing_services_registry = registry
    specialized_service.processing_services_registry = registry
    task_worker_manager(
        primary_service,
        MagicMock(spec=ChatInterface),
        register_delegation_handler=True,
    )

    # Execute delegation - primary profile delegates to specialized profile
    user_query = "Please delegate this task: " + DELEGATED_TASK_DESCRIPTION

    db_context = Database(engine=db_engine)
    result = await primary_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=str(TEST_CHAT_ID),
        trigger_content_parts=[{"type": "text", "text": user_query}],
        trigger_interface_message_id="msg_delegation_test",
        user_name=TEST_USER_NAME,
        chat_interface=MagicMock(spec=ChatInterface),
        request_confirmation_callback=None,
    )

    assert result.error_traceback is None, (
        f"Error during delegation: {result.error_traceback}"
    )
    # The relayed tool result shows whether the delegation completed inline or
    # was handed off / failed, which is what a missing attachment would mean.
    assert "Here's the camera snapshot I captured for your request." in (
        result.text_reply or ""
    ), f"Delegation did not complete inline: {result.text_reply}"

    attachment_ids = result.attachment_ids
    assert attachment_ids is not None and len(attachment_ids) == 1, (
        "Expected the delegated service's camera snapshot to be propagated back "
        f"to the primary profile, got {attachment_ids}"
    )
    attachment_metadata = await attachment_registry.get_attachment(
        Database(engine=db_engine), attachment_ids[0], acting_user_id=None
    )
    assert attachment_metadata is not None
    assert attachment_metadata.mime_type == "image/png"
    assert "camera.test" in attachment_metadata.description
