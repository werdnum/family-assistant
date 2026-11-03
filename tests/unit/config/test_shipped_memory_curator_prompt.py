"""What the shipped memory prompts actually tell a model.

Milestone 2 of docs/design/conversation-memory.md. `test_processing_fail_fast`
already renders every shipped profile's prompt, which is what startup does; this
suite is about the content of the curator prompt memory depends on, and it renders
it the same way rather than reading the YAML, so a placeholder or an escaping
mistake fails here as well.

The phrases pinned below describe the request and edit contract or the guidance
the model needs to follow. They are not a style guide for the rest of the prose.
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
            history_budget_chars=100_000,
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
    assert "Cite them in message_ids and they are appended for you" in curator_prompt


def test_the_curator_is_told_what_makes_an_entry(curator_prompt: str) -> None:
    """Attribution, dating and kind labels are model behaviour, not enforced."""
    assert "One entry is one bullet" in curator_prompt
    assert '"correction:"' in curator_prompt
    assert '"inferred:"' in curator_prompt
    assert "Name the person" in curator_prompt


def test_the_curator_is_told_to_update_rather_than_add(curator_prompt: str) -> None:
    assert "Prefer updating to adding" in curator_prompt
    assert "Never re-add something a person removed" in curator_prompt


def test_the_curator_is_told_what_never_to_remember(curator_prompt: str) -> None:
    assert "What never to remember" in curator_prompt
    assert "Secrets and credentials" in curator_prompt
    assert "meant for one person" in curator_prompt


def test_the_curator_keeps_what_it_is_asked_to_keep(curator_prompt: str) -> None:
    """In prod the curator dismissed stated rules and project steps as one-offs."""
    assert "anything somebody asks to have remembered" in curator_prompt
    assert "A conversation spent on one task is often a step in such a project" in (
        " ".join(curator_prompt.split())
    )
    assert "which is the common case" not in curator_prompt


def test_the_curator_does_not_treat_a_subject_as_private(curator_prompt: str) -> None:
    """Money or tax as a subject is not a reason to drop a household fact."""
    assert "A subject is not private by being about money" in curator_prompt


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
