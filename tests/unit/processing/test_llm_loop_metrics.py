"""The streaming loop attributes a turn's telemetry to its own profile."""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from prometheus_client import REGISTRY

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.call_context import current_processing_profile
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import LocalToolsProvider
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module - note: pylint cannot resolve the implicit `tests` namespace package
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition

PROFILE = "llm-loop-metrics"


def _sample(name: str, labels: Mapping[str, str]) -> float:
    return REGISTRY.get_sample_value(name, dict(labels)) or 0.0


class ProfileRecordingLLMClient(RuleBasedMockLLMClient):
    """Records the profile attribution visible from inside the provider call.

    This is what ``LLMCallTelemetry`` reads to label a call, and a real
    provider client is the only other thing that would exercise it -- so a mock
    that answers the same question proves the plumbing without a network call.
    """

    def __init__(self) -> None:
        super().__init__(rules=[], default_response=LLMOutput(content="ok"))
        self.profiles_seen: list[str | None] = []

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        self.profiles_seen.append(current_processing_profile())
        return await super().generate_response(messages, tools, tool_choice)


def _make_service(llm_client: ProfileRecordingLLMClient) -> ProcessingService:
    return ProcessingService(
        llm_client=llm_client,
        tools_provider=LocalToolsProvider(registrations=[]),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
            id=PROFILE,
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )


@pytest.mark.asyncio
async def test_a_turn_attributes_its_llm_calls_and_counts_itself(
    db_engine: AsyncEngine,
) -> None:
    llm_client = ProfileRecordingLLMClient()
    service = _make_service(llm_client)
    turn_labels = {"profile": PROFILE, "outcome": "success"}
    before = _sample("family_assistant_turns_total", turn_labels)

    result = await service.handle_chat_interaction(
        db_context=Database(db_engine),
        interface_type="web",
        conversation_id="conversation-metrics",
        trigger_content_parts=[{"type": "text", "text": "Hello"}],
        trigger_interface_message_id=None,
        user_name="Test User",
    )

    assert result.status.value == "success"
    assert llm_client.profiles_seen == [PROFILE]
    assert _sample("family_assistant_turns_total", turn_labels) - before == 1


@pytest.mark.asyncio
async def test_the_attribution_does_not_outlive_the_turn(
    db_engine: AsyncEngine,
) -> None:
    """A leaked ContextVar would misattribute every later call in this task."""
    service = _make_service(ProfileRecordingLLMClient())

    await service.handle_chat_interaction(
        db_context=Database(db_engine),
        interface_type="web",
        conversation_id="conversation-metrics-leak",
        trigger_content_parts=[{"type": "text", "text": "Hello"}],
        trigger_interface_message_id=None,
        user_name="Test User",
    )

    assert current_processing_profile() is None
