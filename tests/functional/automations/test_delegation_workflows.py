"""Complex delegation workflows and edge cases with attachments."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import (
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.processing import (
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
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


@pytest.mark.asyncio
async def test_delegate_to_service_with_attachments(
    db_engine: AsyncEngine,
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
    async with DatabaseContext(engine=db_engine) as db_context:
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

    # Create LLM client that expects attachment in delegated request
    def attachment_delegation_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False

        # Check if this is the delegated request containing attachment reference
        last_message = messages[-1]
        content = last_message.content or ""
        return "DELEGATED_TASK_DESCRIPTION" in content and "test_image.png" in content

    llm_client = RuleBasedMockLLMClient(
        rules=[
            (
                attachment_delegation_matcher,
                MockLLMOutput(
                    content="I can see the test image attachment and will process the delegated task accordingly.",
                    tool_calls=None,
                ),
            )
        ],
        default_response=MockLLMOutput(
            content="Processed delegation request (no attachments detected).",
            tool_calls=None,
        ),
    )

    # Create primary service that will call delegate_to_service with attachments
    primary_llm_client = RuleBasedMockLLMClient(
        rules=[
            (
                lambda kwargs: True,  # Match any request
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
                                    "attachment_ids": [test_attachment_id],
                                }),
                            ),
                        )
                    ],
                ),
            )
        ]
    )

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
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        attachment_registry=attachment_registry,
    )

    specialized_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=LocalToolsProvider(definitions=[], implementations={}),
        service_config=ProcessingServiceConfig(
            id=SPECIALIZED_PROFILE_ID,
            prompts={"system_prompt": "I am a specialized assistant."},
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
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
    primary_service.set_processing_services_registry(registry)
    specialized_service.set_processing_services_registry(registry)

    # Execute delegation with attachments
    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    async with DatabaseContext(engine=db_engine) as db_context:
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

        final_reply = result.text_reply
        error = result.error_traceback

    assert error is None, f"Error during attachment delegation: {error}"
    assert final_reply is not None
    assert "delegate this task with the attachment" in final_reply

    # Verify that the delegated LLM actually saw the attachment metadata
    specialized_llm_calls = llm_client.get_calls()
    assert len(specialized_llm_calls) > 0, "No LLM calls made to specialized service"

    # Get the messages sent to the delegated LLM
    messages_to_specialized = specialized_llm_calls[0]["kwargs"]["messages"]

    # Verify attachment was properly injected into LLM messages
    found_attachment_injection = False
    for msg in messages_to_specialized:
        if msg.role == "user":
            content = msg.content or ""
            # Check for attachment injection markers that should be present
            # when an attachment is properly processed
            if isinstance(content, str) and (
                f"[Attachment ID: {test_attachment_id}]" in content
                or ("[System:" in content and test_attachment_id in content)
            ):
                found_attachment_injection = True
                logger.info(f"Found attachment injection in message: {content[:200]}")
                break

    assert found_attachment_injection, (
        f"Attachment {test_attachment_id} was not properly injected into delegated LLM messages. "
        f"Expected to find '[Attachment ID: {test_attachment_id}]' or similar marker "
        f"but messages were: {json.dumps(messages_to_specialized, indent=2)}"
    )

    logger.info("Attachment delegation test completed successfully")


@pytest.mark.asyncio
async def test_delegate_to_service_cross_conversation_attachment_allowed(
    db_engine: AsyncEngine,
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
    async with DatabaseContext(engine=db_engine) as db_context:
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

    def initial_user_request_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last_message = messages[-1]
        return last_message.role == "user" and DELEGATED_TASK_DESCRIPTION in (
            last_message.content or ""
        )

    def tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool"
            and "I can see the attachment" in (msg.content or "")
            for msg in messages
        )

    primary_llm_client = RuleBasedMockLLMClient(
        rules=[
            # Rule: Handle initial user request and attempt delegation
            (
                initial_user_request_matcher,
                MockLLMOutput(
                    content="I'll delegate this task with the attachment from another conversation.",
                    tool_calls=[
                        ToolCallItem(
                            id="delegate_call_security_test",
                            type="function",
                            function=ToolCallFunction(
                                name="delegate_to_service",
                                arguments=json.dumps({
                                    "target_service_id": SPECIALIZED_PROFILE_ID,
                                    "user_request": DELEGATED_TASK_DESCRIPTION,
                                    "confirm_delegation": False,
                                    "attachment_ids": [other_attachment_id],
                                }),
                            ),
                        )
                    ],
                ),
            ),
            # Rule: Handle tool result from specialized service
            (
                tool_result_matcher,
                MockLLMOutput(
                    content="I can see the attachment even though it was from another conversation.",
                    tool_calls=None,
                ),
            ),
        ],
        default_response=MockLLMOutput(
            content="Delegation completed successfully.",
            tool_calls=None,
        ),
    )

    # Create specialized service
    specialized_llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(
            content="I can see the attachment even though it was from another conversation.",
            tool_calls=None,
        ),
    )

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
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
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
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
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
    primary_service.set_processing_services_registry(registry)
    specialized_service.set_processing_services_registry(registry)

    # Execute delegation
    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    async with DatabaseContext(engine=db_engine) as db_context:
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

        final_reply = result.text_reply
        error = result.error_traceback

    # Verify delegation succeeded
    assert error is None
    assert final_reply is not None
    assert "I can see the attachment" in final_reply

    logger.info("Cross-conversation attachment allowed test completed successfully")


@pytest.mark.asyncio
async def test_delegate_to_service_propagates_generated_attachments(
    db_engine: AsyncEngine,
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
        last_message = messages[-1]
        return last_message.role == "user" and DELEGATED_TASK_DESCRIPTION in (
            last_message.content or ""
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
        last_message = messages[-1]
        return (
            last_message.role == "user"
            and "delegate" in (last_message.content or "").lower()
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
            # After delegation completes, primary LLM gets the response WITH attachment references
            (
                lambda kwargs: any(
                    msg.role == "tool" and msg.name == "delegate_to_service"
                    for msg in kwargs.get("messages", [])
                ),
                MockLLMOutput(
                    content="The specialized service has completed your request with a camera snapshot.",
                    tool_calls=None,
                ),
            ),
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
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
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
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
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
    primary_service.set_processing_services_registry(registry)
    specialized_service.set_processing_services_registry(registry)

    # Execute delegation - primary profile delegates to specialized profile
    user_query = "Please delegate this task: " + DELEGATED_TASK_DESCRIPTION

    async with DatabaseContext(engine=db_engine) as db_context:
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

        final_reply = result.text_reply
        error = result.error_traceback
        attachment_ids = result.attachment_ids

    assert error is None, f"Error during delegation: {error}"
    assert final_reply is not None
    assert "specialized service" in final_reply.lower()

    # KEY ASSERTION: Verify that attachments from the delegated service are propagated back
    assert attachment_ids is not None and len(attachment_ids) > 0, (
        "Expected attachment IDs from delegated service to be propagated back to primary profile"
    )

    # Verify the attachment exists in the registry
    async with DatabaseContext(engine=db_engine) as db_context:
        for att_id in attachment_ids:
            attachment_metadata = await attachment_registry.get_attachment(
                db_context, att_id
            )
            assert attachment_metadata is not None
            assert (
                attachment_metadata.mime_type == "image/png"
            )  # mock_camera_snapshot returns PNG
            logger.info(f"Verified propagated attachment: {att_id}")

    logger.info("Delegation attachment propagation test completed successfully")
