import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import get_db_context
from family_assistant.tools import ToolExecutionContext
from family_assistant.tools.types import ToolResult
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)


class SimpleToolsProvider:
    async def get_tool_definitions(self) -> list:
        return []

    async def execute_tool(
        self,
        name: str,
        arguments: dict,
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return ""

    async def close(self) -> None:
        pass


# Default configuration for tests
DEFAULT_CONFIG = {
    "telegram": {
        "bot_token": "fake_token",
        "bot_name": "TestBot",
        "allowed_chat_ids": [12345],
    },
    "server": {"url": "http://testserver"},
    "default_profile_settings": {
        "processing_config": {
            "prompts": {"system_prompt": "You are a helpful assistant."},
            "timezone": "UTC",
            "max_history_messages": 10,
            "history_max_age_hours": 24,
            "llm_model": "fake_model",
            "delegation_security_level": "confirm",
        },
        "tools_config": {"enable_local_tools": [], "enable_mcp_server_ids": []},
    },
    "service_profiles": [
        {
            "id": "profile_a",
            "description": "Profile A",
            "processing_config": {"prompts": {"system_prompt": "You are Profile A."}},
        },
        {
            "id": "profile_b",
            "description": "Profile B",
            "processing_config": {"prompts": {"system_prompt": "You are Profile B."}},
        },
    ],
    "default_service_profile_id": "profile_a",
}


@pytest.mark.asyncio
async def test_reply_with_different_profile_includes_history(
    db_engine: AsyncEngine,
) -> None:
    """
    Test that when a user replies to a message from one profile,
    and the reply is handled by another profile, the full thread history is included.
    """
    # --- Setup ---
    chat_id = 12345
    user_name = "Test User"
    initial_message_id = "660"
    reply_message_id = "662"

    # --- LLM Mocks ---
    def profile_a_matcher(kwargs: MatcherArgs) -> bool:
        return get_last_message_text(kwargs["messages"]) == "Hello"

    profile_a_response = MockLLMOutput(content="Hello from Profile A")
    llm_client_a = RuleBasedMockLLMClient(
        rules=[(profile_a_matcher, profile_a_response)]
    )

    def profile_b_matcher(kwargs: MatcherArgs) -> bool:
        return get_last_message_text(kwargs["messages"]) == "Good job"

    profile_b_response = MockLLMOutput(content="Hello from Profile B")
    llm_client_b = RuleBasedMockLLMClient(
        rules=[(profile_b_matcher, profile_b_response)]
    )

    # --- Processing Services ---
    profile_a_config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are Profile A."},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config={"enable_local_tools": [], "enable_mcp_server_ids": []},
        delegation_security_level="confirm",
        id="profile_a",
    )
    profile_a_service = ProcessingService(
        llm_client=llm_client_a,
        tools_provider=SimpleToolsProvider(),
        service_config=profile_a_config,
        context_providers=[],
        server_url="http://testserver",
        app_config={},
    )

    profile_b_config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are Profile B."},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config={"enable_local_tools": [], "enable_mcp_server_ids": []},
        delegation_security_level="confirm",
        id="profile_b",
    )
    profile_b_service = ProcessingService(
        llm_client=llm_client_b,
        tools_provider=SimpleToolsProvider(),
        service_config=profile_b_config,
        context_providers=[],
        server_url="http://testserver",
        app_config={},
    )
    # --- 1. Simulate initial message from Profile A ---
    async with get_db_context(db_engine) as db_context:
        result = await profile_a_service.handle_chat_interaction(
            db_context=db_context,
            interface_type="telegram",
            conversation_id=str(chat_id),
            trigger_content_parts=[{"type": "text", "text": "Hello"}],
            trigger_interface_message_id=initial_message_id,
            user_name=user_name,
        )
        # Manually update the interface_message_id for the assistant's reply
        initial_assistant_message_id = result.assistant_message_internal_id
        if initial_assistant_message_id:
            await db_context.message_history.update_interface_id(
                internal_id=initial_assistant_message_id,
                interface_message_id="661",  # The bot replies with a new message id
            )

    # --- 2. Simulate a reply from the user, handled by Profile B ---
    async with get_db_context(db_engine) as db_context:
        await profile_b_service.handle_chat_interaction(
            db_context=db_context,
            interface_type="telegram",
            conversation_id=str(chat_id),
            trigger_content_parts=[{"type": "text", "text": "Good job"}],
            trigger_interface_message_id=reply_message_id,
            user_name=user_name,
            replied_to_interface_id=initial_message_id,
        )

    # --- 3. Assertions ---
    # Check that Profile B's LLM was called
    assert len(llm_client_b.get_calls()) == 1

    # Get the messages passed to Profile B's LLM
    call_args = llm_client_b.get_calls()[0]["kwargs"]
    messages_for_llm = call_args["messages"]

    # Assert that the history from Profile A is present
    # Should include: system prompt, root user message, Profile A's assistant response, current user message
    assert len(messages_for_llm) >= 4
    assert messages_for_llm[0].role == "system"
    assert "You are Profile B" in messages_for_llm[0].content

    # Check that the root user message is included (for attachment context)
    assert messages_for_llm[1].role == "user"
    assert "Hello" in messages_for_llm[1].content

    # Check for Profile A's assistant message
    assert messages_for_llm[2].role == "assistant"
    assert "Hello from Profile A" in messages_for_llm[2].content

    # Check for the current user's reply
    assert messages_for_llm[3].role == "user"
    assert "Good job" in messages_for_llm[3].content
