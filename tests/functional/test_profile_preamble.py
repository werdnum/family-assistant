"""Functional tests for processing profile preamble in system prompt.

Verifies that handle_chat_interaction injects an identifying preamble into the
system prompt so the model knows which processing profile is active and that
the user explicitly selected it.
"""

import logging
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.content_parts import text_content
from family_assistant.llm.messages import SystemMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.tools import LocalToolsProvider
from tests.mocks.mock_llm import LLMOutput as MockLLMOutput
from tests.mocks.mock_llm import RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


def _make_service(
    profile_id: str,
    description: str = "",
    system_prompt: str = "You are a test assistant for {user_name}.",
) -> tuple[ProcessingService, RuleBasedMockLLMClient]:
    """Create a ProcessingService with a mock LLM for testing."""
    config = ProcessingServiceConfig(
        prompts={"system_prompt": system_prompt},
        timezone=ZoneInfo("UTC"),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.BLOCKED,
        id=profile_id,
        description=description,
    )
    mock_llm = RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(content="OK", tool_calls=None),
    )
    tools_provider = LocalToolsProvider(
        definitions=[],
        implementations={},
    )
    service = ProcessingService(
        llm_client=mock_llm,
        tools_provider=tools_provider,
        service_config=config,
        context_providers=[],
        server_url="http://localhost:8000",
        app_config=AppConfig(),
        credential_resolvers=None,
        api_backend=None,
    )
    return service, mock_llm


def _get_system_prompt_from_calls(mock_llm: RuleBasedMockLLMClient) -> str:
    """Extract the system prompt from the first LLM call."""
    calls = mock_llm.get_calls()
    assert calls, "Expected at least one LLM call"
    messages = calls[0]["kwargs"]["messages"]
    system_messages = [m for m in messages if isinstance(m, SystemMessage)]
    assert system_messages, "Expected a SystemMessage in the LLM call"
    return system_messages[0].content


class TestProfilePreambleInSystemPrompt:
    """Test that the profile preamble is injected into system prompts."""

    @pytest.mark.asyncio
    async def test_preamble_contains_profile_id(self, db_engine: AsyncEngine) -> None:
        """The system prompt should identify the active processing profile."""
        service, mock_llm = _make_service("engineer")

        db_context = Database(engine=db_engine)
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-1",
            trigger_content_parts=[text_content("hello")],
            trigger_interface_message_id="msg-1",
            user_name="TestUser",
        )

        system_prompt = _get_system_prompt_from_calls(mock_llm)
        assert "[Active Processing Profile: engineer]" in system_prompt

    @pytest.mark.asyncio
    async def test_preamble_states_user_explicitly_selected(
        self, db_engine: AsyncEngine
    ) -> None:
        """The system prompt should indicate the user explicitly selected the profile."""
        service, mock_llm = _make_service("camera_analyst")

        db_context = Database(engine=db_engine)
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-2",
            trigger_content_parts=[text_content("check the front door")],
            trigger_interface_message_id="msg-2",
            user_name="TestUser",
        )

        system_prompt = _get_system_prompt_from_calls(mock_llm)
        assert (
            'user has explicitly selected the "camera_analyst" processing profile'
            in system_prompt
        )

    @pytest.mark.asyncio
    async def test_preamble_includes_description(self, db_engine: AsyncEngine) -> None:
        """When a profile has a description, it should appear in the preamble."""
        service, mock_llm = _make_service(
            "engineer",
            description="Read-only diagnostic access to source code and database",
        )

        db_context = Database(engine=db_engine)
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-3",
            trigger_content_parts=[text_content("debug this")],
            trigger_interface_message_id="msg-3",
            user_name="TestUser",
        )

        system_prompt = _get_system_prompt_from_calls(mock_llm)
        assert (
            "Read-only diagnostic access to source code and database" in system_prompt
        )

    @pytest.mark.asyncio
    async def test_preamble_omits_description_line_when_empty(
        self, db_engine: AsyncEngine
    ) -> None:
        """When a profile has no description, the description line should be absent."""
        service, mock_llm = _make_service("minimal_profile", description="")

        db_context = Database(engine=db_engine)
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-4",
            trigger_content_parts=[text_content("test")],
            trigger_interface_message_id="msg-4",
            user_name="TestUser",
        )

        system_prompt = _get_system_prompt_from_calls(mock_llm)
        assert "Profile purpose:" not in system_prompt
        assert "[Active Processing Profile: minimal_profile]" in system_prompt

    @pytest.mark.asyncio
    async def test_preamble_precedes_profile_system_prompt(
        self, db_engine: AsyncEngine
    ) -> None:
        """The preamble should appear before the profile's own system prompt."""
        service, mock_llm = _make_service(
            "default_assistant",
            description="Main assistant",
            system_prompt="You are a helpful family assistant for {user_name}.",
        )

        db_context = Database(engine=db_engine)
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-5",
            trigger_content_parts=[text_content("hi")],
            trigger_interface_message_id="msg-5",
            user_name="TestUser",
        )

        system_prompt = _get_system_prompt_from_calls(mock_llm)
        preamble_pos = system_prompt.index("[Active Processing Profile:")
        body_pos = system_prompt.index("You are a helpful family assistant")
        assert preamble_pos < body_pos

    @pytest.mark.asyncio
    async def test_preamble_includes_scope_warning(
        self, db_engine: AsyncEngine
    ) -> None:
        """The preamble should warn the model not to exceed its profile scope."""
        service, mock_llm = _make_service("engineer")

        db_context = Database(engine=db_engine)
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-6",
            trigger_content_parts=[text_content("investigate")],
            trigger_interface_message_id="msg-6",
            user_name="TestUser",
        )

        system_prompt = _get_system_prompt_from_calls(mock_llm)
        assert "Do not attempt actions outside your profile's scope" in system_prompt


class TestProfilePreambleInStream:
    """Test that handle_chat_interaction_stream also injects the preamble."""

    @pytest.mark.asyncio
    async def test_stream_preamble_matches_sync(self, db_engine: AsyncEngine) -> None:
        """The streaming path should inject the same preamble as the sync path."""
        service, mock_llm = _make_service(
            "engineer",
            description="Read-only diagnostic access",
        )

        db_context = Database(engine=db_engine)
        async for _ in service.handle_chat_interaction_stream(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-stream-1",
            trigger_content_parts=[text_content("hello")],
            trigger_interface_message_id="msg-stream-1",
            user_name="TestUser",
        ):
            pass

        system_prompt = _get_system_prompt_from_calls(mock_llm)
        assert "[Active Processing Profile: engineer]" in system_prompt
        assert (
            'user has explicitly selected the "engineer" processing profile'
            in system_prompt
        )
        assert "Read-only diagnostic access" in system_prompt
        assert "Do not attempt actions outside your profile's scope" in system_prompt
