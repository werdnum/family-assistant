"""A real ``ProcessingService`` for components that hold one but run no LLM turn.

Task workers that only index or embed still require a processing service. This
one is real, with no context providers and no tools unless given; its LLM client
fails the test on any call, so a turn that unexpectedly reaches it cannot pass
silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.tools.infrastructure import LocalToolsProvider, ToolsProvider
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.llm import LLMOutput
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition


class IdleLLMClient(RuleBasedMockLLMClient):
    """An LLM client for a service that must never run a turn."""

    def __init__(self) -> None:
        super().__init__(rules=[])

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        raise AssertionError(
            f"idle processing service unexpectedly ran an LLM turn ({len(messages)} messages)"
        )


def make_idle_processing_service(
    *,
    profile_id: str = "idle_test_profile",
    timezone: str = "UTC",
    app_config: AppConfig | None = None,
    tools_provider: ToolsProvider | None = None,
) -> ProcessingService:
    """A minimal real ``ProcessingService`` whose LLM fails on any turn.

    It has no tools unless ``tools_provider`` is given, for components that
    find tools through the service rather than running a turn.
    """
    return ProcessingService(
        llm_client=IdleLLMClient(),
        tools_provider=(
            tools_provider
            if tools_provider is not None
            else LocalToolsProvider(definitions=[], implementations={})
        ),
        service_config=ProcessingServiceConfig(
            id=profile_id,
            prompts={"system_prompt": "You are a helpful assistant."},
            timezone=ZoneInfo(timezone),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        context_providers=[],
        server_url=None,
        app_config=app_config if app_config is not None else AppConfig(),
    )
