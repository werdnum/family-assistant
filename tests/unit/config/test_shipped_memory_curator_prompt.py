"""What the shipped memory prompts actually tell a model.

Milestone 2 of docs/design/conversation-memory.md. `test_processing_fail_fast`
already renders every shipped profile's prompt, which is what startup does; this
suite is about the content of the two prompts memory depends on, and it renders
them the same way rather than reading the YAML, so a placeholder or an escaping
mistake fails here as well.

The phrases pinned below are load-bearing: each is something the M1 code
enforces or expects, so a rewrite that drops one leaves the prompt and the code
disagreeing. They are not a style guide for the rest of the prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from family_assistant.tools.types import (
        ToolDefinition,
        ToolExecutionContext,
        ToolResult,
    )

pytestmark = pytest.mark.no_db


class _NoTools:
    """The narrowest tools provider a ProcessingService will accept.

    Rendering a prompt calls none of these; the service just requires one.
    """

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
        raise NotImplementedError(name)

    async def close(self) -> None:
        return None


def _render(prompts: dict[str, str], profile_id: str) -> str:
    """One profile's system prompt, through the path startup validates."""
    service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="ok")
        ),
        tools_provider=_NoTools(),
        service_config=ProcessingServiceConfig(
            prompts=dict(prompts),
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id=profile_id,
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )
    service.validate_system_prompt_renders()
    return service.format_system_prompt(user_name="tester")


@pytest.fixture(name="curator_prompt")
def curator_prompt_fixture(shipped_config: AppConfig) -> str:
    profile = shipped_profile(shipped_config, "memory_curator")
    return _render(profile.processing_config.prompts, profile.id)


@pytest.fixture(name="assistant_prompt")
def assistant_prompt_fixture(shipped_config: AppConfig) -> str:
    profile = shipped_profile(shipped_config, "default_assistant")
    return _render(profile.processing_config.prompts, profile.id)


def test_the_curator_is_told_the_shape_of_the_request_it_is_given(
    curator_prompt: str,
) -> None:
    """The review task renders these three things; the prompt must name them.

    `memory/review.py` puts the transcript under "Conversation under review"
    and the topic notes under "Memory entries you may update", and leaves the
    core note to the notes context provider.
    """
    assert "Conversation under review" in curator_prompt
    assert "Memory entries you may update" in curator_prompt
    assert "always-loaded note" in curator_prompt
    assert "get_note" in curator_prompt


def test_the_curator_is_told_how_to_cite_evidence(curator_prompt: str) -> None:
    """The apply path refuses an uncited edit and an id outside the stretch."""
    assert "message_ids" in curator_prompt
    assert "never invent one" in curator_prompt
    assert "A move cites nothing" in curator_prompt
    assert "(refs: #412)" in curator_prompt


def test_the_curator_is_told_what_makes_an_entry(curator_prompt: str) -> None:
    """Attribution, dating and kind labels are model behaviour, not enforced."""
    assert "One entry is one bullet" in curator_prompt
    assert '"correction:"' in curator_prompt
    assert '"inferred:"' in curator_prompt
    assert "Alice prefers the tram" in curator_prompt


def test_the_curator_is_told_to_update_rather_than_add(curator_prompt: str) -> None:
    assert "Prefer updating to adding" in curator_prompt
    assert "Never re-add something a person removed" in curator_prompt


def test_the_curator_is_told_what_never_to_remember(curator_prompt: str) -> None:
    assert "What never to remember" in curator_prompt
    assert "Secrets and credentials" in curator_prompt
    assert "meant for one person" in curator_prompt


def test_the_curator_is_told_the_retry_budget_and_the_empty_reply(
    curator_prompt: str,
) -> None:
    """`tools/memory.py` refuses a third proposal outright; the prompt agrees.

    And a review that proposes nothing ends as `no_changes`, which is the
    common case rather than a failure.
    """
    assert "one corrected list" in curator_prompt
    assert "do not send a third" in curator_prompt
    assert "propose nothing and reply in one line" in curator_prompt


def test_the_curator_is_told_memory_is_household_wide(curator_prompt: str) -> None:
    assert "Memory is household-wide" in curator_prompt


def test_the_assistant_is_told_how_to_reach_memory(assistant_prompt: str) -> None:
    """Conditional on the tool, because shipped profiles do not hold it yet."""
    assert "Household memory:" in assistant_prompt
    assert "If `propose_memory_edits` is among your tools" in assistant_prompt
    assert "rather than `add_or_update_note`" in assistant_prompt
    assert "core memory note is in your context every turn" in assistant_prompt


def test_the_assistant_is_told_how_to_honour_a_forget(assistant_prompt: str) -> None:
    """Forgetting in v1 removes the entry, and nothing else."""
    assert "is a `remove` edit" in assistant_prompt
    assert "removed from memory but not from the" in assistant_prompt
    assert "come back if somebody says it again" in assistant_prompt


def test_the_assistant_is_told_memory_is_household_wide(
    assistant_prompt: str,
) -> None:
    assert "Memory is household-wide" in assistant_prompt
