"""Tests for the request-stable prefix of the rendered system prompt.

The prefix drives where providers place a prompt-cache breakpoint. If it ever
covers per-turn material the cache silently stops hitting, so these tests pin
the property that actually matters: the prefix does not move or change when the
per-turn inputs do.
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


DEFAULT_TEMPLATE = (
    "You are a helper with a long stable preamble.\n\n"
    "# Context\nCurrent time: {current_time}\n\n{aggregated_other_context}\n"
)


def _make_service(
    template: str = DEFAULT_TEMPLATE,
    clock: MockClock | None = None,
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
    )
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="ok")
        ),
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        clock=clock or MockClock(datetime(2026, 7, 25, 10, 0, 0, tzinfo=UTC)),
    )


@pytest.mark.no_db
class TestStableSystemPromptPrefix:
    def test_prefix_excludes_the_timestamp(self) -> None:
        service = _make_service()

        rendered, prefix_len = service._render_system_prompt("tester", "")

        assert prefix_len is not None
        assert "2026-07-25 10:00:00" in rendered
        assert "2026-07-25 10:00:00" not in rendered[:prefix_len]

    def test_prefix_is_identical_across_different_clock_times(self) -> None:
        """The whole point: the cached block must survive the clock advancing."""
        early = _make_service(
            clock=MockClock(datetime(2026, 7, 25, 10, 0, 0, tzinfo=UTC))
        )
        later = _make_service(
            clock=MockClock(datetime(2026, 7, 25, 23, 59, 59, tzinfo=UTC))
        )

        early_rendered, early_len = early._render_system_prompt("tester", "")
        later_rendered, later_len = later._render_system_prompt("tester", "")

        assert early_len == later_len
        assert early_rendered[:early_len] == later_rendered[:later_len]
        assert early_rendered != later_rendered

    def test_prefix_is_identical_across_different_aggregated_context(self) -> None:
        service = _make_service()

        empty_rendered, empty_len = service._render_system_prompt("tester", "")
        full_rendered, full_len = service._render_system_prompt(
            "tester", "Calendar:\n- 09:00 standup"
        )

        assert empty_len == full_len
        assert empty_rendered[:empty_len] == full_rendered[:full_len]

    def test_prefix_covers_the_bulk_of_a_realistic_prompt(self) -> None:
        """A breakpoint that lands early enough to be worthless is a silent bug."""
        service = _make_service()

        rendered, prefix_len = service._render_system_prompt(
            "tester", "Calendar:\n- 09:00 standup"
        )

        assert prefix_len is not None
        assert prefix_len > len(rendered) * 0.5

    def test_template_without_volatile_placeholders_is_fully_stable(self) -> None:
        service = _make_service(template="You are a helper. Nothing varies here.")

        rendered, prefix_len = service._render_system_prompt("tester", "")

        assert prefix_len == len(rendered)

    def test_boundary_stops_at_the_first_volatile_byte(self) -> None:
        """With the timestamp first in the template, only the profile preamble
        (which the service prepends ahead of it) is stable, and the boundary
        must land there rather than swallowing the timestamp."""
        service = _make_service(template="{current_time} then the rest of the prompt.")

        rendered, prefix_len = service._render_system_prompt("tester", "")

        assert prefix_len is not None
        assert "2026-07-25 10:00:00" not in rendered[:prefix_len]
        assert rendered[prefix_len:].startswith("2026-07-25 10:00:00")
