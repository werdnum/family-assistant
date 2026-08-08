"""Tests for the system prompt's request-stable prefix.

The prefix drives where providers place a prompt-cache breakpoint, and for the
providers that cache implicitly it is simply how much of the request survives
from one turn to the next. Per-turn material now rides in the trailing
``<turn_context>`` block instead of the prompt, so what these tests pin is that
nothing volatile has crept back into the prompt: it must render identically no
matter what the clock says or what the context providers report.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.utils.clock import MockClock
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from family_assistant.tools import ToolExecutionContext
    from family_assistant.tools.types import ToolDefinition, ToolResult


class SimpleToolsProvider:
    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return []

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool args are dynamic
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return ""

    async def close(self) -> None:
        pass


class StaticContextProvider:
    """A context provider whose fragments the test controls."""

    def __init__(self, fragment: str) -> None:
        self._fragment = fragment

    @property
    def name(self) -> str:
        return "notes"

    async def get_context_fragments(self) -> list[str]:
        return [self._fragment]


DEFAULT_TEMPLATE = "You are a helper with a long stable preamble for {user_name}."


def _make_service(
    template: str = DEFAULT_TEMPLATE,
    clock: MockClock | None = None,
    context_fragment: str | None = None,
    include_aggregated_context: bool = False,
) -> ProcessingService:
    config = ProcessingServiceConfig(
        prompts={"system_prompt": template},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="cache_prefix_test",
        max_iterations=5,
        include_aggregated_context=include_aggregated_context,
    )
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="ok")
        ),
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=(
            [StaticContextProvider(context_fragment)]
            if context_fragment is not None
            else []
        ),
        server_url="http://testserver",
        app_config=AppConfig(),
        clock=clock or MockClock(datetime(2026, 7, 25, 10, 0, 0, tzinfo=UTC)),
    )


@pytest.mark.no_db
class TestSystemPromptIsFullyStable:
    def test_prompt_carries_no_timestamp(self) -> None:
        service = _make_service()

        rendered = service.format_system_prompt(user_name="tester")

        assert "2026-07-25 10:00:00" not in rendered

    def test_prompt_is_identical_across_different_clock_times(self) -> None:
        """The whole point: the cached block must survive the clock advancing."""
        early = _make_service(
            clock=MockClock(datetime(2026, 7, 25, 10, 0, 0, tzinfo=UTC))
        )
        later = _make_service(
            clock=MockClock(datetime(2026, 7, 25, 23, 59, 59, tzinfo=UTC))
        )

        assert early.format_system_prompt(
            user_name="tester"
        ) == later.format_system_prompt(user_name="tester")

    def test_prompt_is_identical_regardless_of_what_providers_report(self) -> None:
        """Provider output is per-turn; it must not reach the prompt at all.

        Both services are granted the context, so the only difference is what the
        providers have to say -- which is exactly what used to be interpolated.
        """
        empty = _make_service(include_aggregated_context=True)
        full = _make_service(
            context_fragment="Calendar:\n- 09:00 standup",
            include_aggregated_context=True,
        )

        assert empty.format_system_prompt(
            user_name="tester"
        ) == full.format_system_prompt(user_name="tester")

    def test_breakpoint_covers_the_whole_prompt(self) -> None:
        service = _make_service()

        rendered = service.format_system_prompt(user_name="tester")
        message = service._build_system_message(rendered)

        assert message.stable_prefix_len == len(rendered)

    def test_empty_prompt_reports_no_breakpoint(self) -> None:
        """A zero-length prefix is not a breakpoint; it must be None, not 0."""
        message = ProcessingService._build_system_message("")

        assert message.stable_prefix_len is None


@pytest.mark.no_db
class TestRemovedPlaceholdersFailLoudly:
    @pytest.mark.parametrize(
        "placeholder", ["{current_time}", "{aggregated_other_context}"]
    )
    def test_rendering_a_removed_placeholder_raises(self, placeholder: str) -> None:
        """Silently rendering these would put volatile text back in the prompt."""
        service = _make_service(template=f"You are a helper. {placeholder}")

        with pytest.raises(ValueError, match="unknown placeholders"):
            service.format_system_prompt(user_name="tester")

    def test_startup_validation_rejects_a_removed_placeholder(self) -> None:
        service = _make_service(template="You are a helper. {current_time}")

        with pytest.raises(ValueError, match="current_time"):
            service.validate_system_prompt_renders()

    def test_startup_validation_accepts_a_good_template(self) -> None:
        service = _make_service(template="Helping {user_name} at {server_url}.")

        service.validate_system_prompt_renders()
