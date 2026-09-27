"""Functional tests for processing profile preamble in system prompt.

Verifies that handle_chat_interaction injects an identifying header into the
system prompt so the model knows which processing profile is active -- and that
it injects nothing else. The profile's ``description`` is the caller-facing
catalog entry and is addressed to whoever is choosing a profile, so it must not
reach the profile's own instructions.
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
    async def test_preamble_excludes_caller_facing_description(
        self, db_engine: AsyncEngine
    ) -> None:
        """The routing blurb is addressed to the caller, not to this profile."""
        service, mock_llm = _make_service(
            "coder",
            description=(
                "Coding agent. Choose it over spawn_worker when the task needs no "
                "files from the shared workspace."
            ),
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
        assert "spawn_worker" not in system_prompt
        assert "Profile purpose:" not in system_prompt
        assert "[Active Processing Profile: coder]" in system_prompt

    @pytest.mark.asyncio
    async def test_preamble_is_the_identity_line_alone(
        self, db_engine: AsyncEngine
    ) -> None:
        """Everything ahead of the profile's own prompt is the one header line.

        No description, no claim about how the profile was reached (a delegated
        run and a slash command arrive identically), and no scope warning (the
        tool policy enforces scope, and some profiles hold no tools at all).
        """
        service, mock_llm = _make_service(
            "minimal_profile",
            description="Some catalog blurb",
            system_prompt="You are a test assistant for {user_name}.",
        )

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
        head, body_marker, _ = system_prompt.partition("You are a test assistant")
        assert body_marker, "Expected the profile's own prompt in the system prompt"
        assert head.strip() == "[Active Processing Profile: minimal_profile]"
        assert "Some catalog blurb" not in system_prompt
        assert "explicitly selected" not in system_prompt
        assert "outside your profile's scope" not in system_prompt


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
        head, body_marker, _ = system_prompt.partition("You are a test assistant")
        assert body_marker, "Expected the profile's own prompt in the system prompt"
        assert head.strip() == "[Active Processing Profile: engineer]"
        assert "Read-only diagnostic access" not in system_prompt
