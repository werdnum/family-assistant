"""The shipped tiers that run in Claude Code, and the rules that keep them honest.

A tier with ``runtime: "claude_code"`` runs its primary model in a Claude
Code session, billed to the Claude subscription, wherever the deployment
provides a token. Each misconfiguration refused here would otherwise run
quietly as something else: a non-Anthropic model the session cannot run, a
parameter dropped on the floor, or a profile setting the session ignores.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from pydantic import ValidationError

from family_assistant.assistant import Assistant, validate_claude_code_profile
from family_assistant.config_loader import load_config
from family_assistant.config_models import (
    AppConfig,
    ModelTierConfig,
    ProcessingConfig,
    RetryModelConfig,
)
from family_assistant.llm.deferred_tools import ACTIVATE_TOOLS_TOOL_NAME
from family_assistant.llm.model_tiers import resolve_claude_agent_model
from family_assistant.processing.claude_agent_loop import CLAUDE_CODE_OAUTH_TOKEN_ENV
from family_assistant.processing.service import ProcessingService
from family_assistant.security.taint import SinkClass
from family_assistant.storage.database import Database
from tests.mocks.fake_claude_code import FakeClaudeCode, FakeResponse, FakeText

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

PROFILE_ID = "complex_tasks"


@pytest.fixture(name="shipped")
def shipped_fixture() -> AppConfig:
    return load_config(
        config_file_path="nonexistent-so-only-defaults.yaml", load_dotenv_file=False
    )


def test_deep_runs_opus_in_claude_code_at_high_effort(shipped: AppConfig) -> None:
    tier = shipped.model_tiers["deep"]
    model = resolve_claude_agent_model("deep", tier, shipped.llm_parameters)

    assert (tier.runtime, model.model, model.effort) == (
        "claude_code",
        "claude-opus-5-5",
        "high",
    )


def test_frontier_runs_fable_in_claude_code_at_high_effort(shipped: AppConfig) -> None:
    tier = shipped.model_tiers["frontier"]
    model = resolve_claude_agent_model("frontier", tier, shipped.llm_parameters)

    assert (tier.runtime, model.model, model.effort) == (
        "claude_code",
        "claude-fable-5-2",
        "high",
    )


@pytest.mark.parametrize(
    ("tier_name", "model_id"),
    [
        ("claude_opus_5_5", "claude-opus-5-5"),
        ("claude_sonnet_5_5", "claude-sonnet-5-5"),
        ("claude_fable_5_2", "claude-fable-5-2"),
    ],
)
def test_claude_presets_run_in_claude_code(
    shipped: AppConfig, tier_name: str, model_id: str
) -> None:
    """A conversation carrying a Claude preset forward keeps to the subscription.

    These were the removed claude_agent profile's tiers, so its migrated
    conversations still name them.
    """
    tier = shipped.model_tiers[tier_name]
    model = resolve_claude_agent_model(tier_name, tier, shipped.llm_parameters)

    assert (tier.runtime, model.model) == ("claude_code", model_id)


def test_claude_code_tier_on_another_provider_is_refused() -> None:
    with pytest.raises(ValidationError, match="Anthropic"):
        ModelTierConfig(
            runtime="claude_code",
            chain=[RetryModelConfig(provider="google", model="gemini-4-argon")],
        )


def test_claude_code_tier_with_flex_is_refused() -> None:
    with pytest.raises(ValidationError, match="flex"):
        ModelTierConfig(
            runtime="claude_code",
            chain=[
                RetryModelConfig(
                    provider="anthropic", model="claude-opus-5-5", flex=True
                )
            ],
        )


def test_parameter_a_session_cannot_apply_is_refused() -> None:
    tier = ModelTierConfig(
        label="Opus",
        chain=[
            RetryModelConfig(
                provider="anthropic",
                model="claude-opus-5-5",
                llm_parameters={"max_tokens": 1000},
            )
        ],
    )

    with pytest.raises(ValueError, match="max_tokens"):
        resolve_claude_agent_model("opus", tier, {})


def test_taint_sink_on_a_profile_reaching_a_claude_code_tier_is_refused() -> None:
    config = ProcessingConfig(
        model_tier="deep", taint_sink_class=SinkClass.SANDBOX_NETWORK
    )

    with pytest.raises(ValueError, match="taint_sink_class"):
        validate_claude_code_profile("sandboxed", config, ["deep"])


async def _start(
    db_engine: AsyncEngine, fake: FakeClaudeCode, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Assistant]:
    for env_var in (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.setenv(env_var, os.getenv(env_var, f"test-{env_var.lower()}"))
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml", load_dotenv_file=False
    )
    config.service_profiles = [p for p in config.service_profiles if p.id == PROFILE_ID]
    config.default_service_profile_id = PROFILE_ID
    config.telegram_enabled = False
    config.mcp_config.mcpServers = {}
    config.embedding_model = "mock-deterministic-embedder"
    config.embedding_dimensions = 10

    def client_factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
        return ClaudeSDKClient(options, transport=fake)

    assistant = Assistant(
        config=config,
        database_engine=db_engine,
        claude_agent_client_factory=client_factory,
    )
    try:
        await assistant.setup_dependencies()
        yield assistant
    finally:
        await assistant.stop_services()


def _service(assistant: Assistant) -> ProcessingService:
    service = assistant.processing_services_registry[PROFILE_ID]
    assert isinstance(service, ProcessingService)
    return service


@pytest.fixture(name="fake")
def fake_fixture() -> FakeClaudeCode:
    return FakeClaudeCode(responses=[FakeResponse([FakeText("Done.")])])


@pytest.fixture(name="started")
async def started_fixture(
    db_engine: AsyncEngine, fake: FakeClaudeCode, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Assistant]:
    """The shipped profile built through normal startup, with a subscription token."""
    monkeypatch.setenv(CLAUDE_CODE_OAUTH_TOKEN_ENV, "test-oauth-token")
    async for assistant in _start(db_engine, fake, monkeypatch):
        yield assistant


@pytest.fixture(name="started_without_token")
async def started_without_token_fixture(
    db_engine: AsyncEngine, fake: FakeClaudeCode, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Assistant]:
    monkeypatch.delenv(CLAUDE_CODE_OAUTH_TOKEN_ENV, raising=False)
    async for assistant in _start(db_engine, fake, monkeypatch):
        yield assistant


@pytest.mark.asyncio
async def test_startup_runs_the_claude_code_tiers_in_sessions(
    started: Assistant,
) -> None:
    loop = _service(started).claude_agent_loop

    assert loop is not None and set(loop.models) == {
        "deep",
        "frontier",
        "claude_opus_5_5",
        "claude_sonnet_5_5",
        "claude_fable_5_2",
    }


@pytest.mark.asyncio
async def test_without_a_token_the_tiers_run_on_their_chain(
    started_without_token: Assistant,
) -> None:
    assert _service(started_without_token).claude_agent_loop is None


@pytest.mark.asyncio
async def test_session_is_offered_household_tools_without_activation(
    started: Assistant, fake: FakeClaudeCode, db_engine: AsyncEngine
) -> None:
    await _service(started).handle_chat_interaction(
        db_context=Database(db_engine),
        interface_type="web",
        conversation_id="conv-1",
        trigger_content_parts=[{"type": "text", "text": "Hello"}],
        trigger_interface_message_id="msg-1",
        user_name="TestUser",
    )

    offered = {tool["name"] for tool in fake.offered_tools}
    assert "add_or_update_note" in offered
    assert ACTIVATE_TOOLS_TOOL_NAME not in offered
