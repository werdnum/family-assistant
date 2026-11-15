"""The shipped `claude_agent` profile, and the rules that keep it what it says.

A Claude Agent SDK profile runs one Claude model in a Claude Code session,
billed to a subscription. Each misconfiguration refused here would otherwise
run quietly as something else: a fallback model that never runs, a parameter
dropped on the floor, an inline model ignored, or a profile advertised on a
deployment that has no subscription token to run it with.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from family_assistant.assistant import Assistant, validate_claude_agent_config
from family_assistant.config_loader import (
    CLAUDE_CODE_OAUTH_TOKEN_ENV,
    drop_unavailable_claude_agent_profiles,
    load_config,
)
from family_assistant.config_models import (
    AppConfig,
    ClaudeAgentConfig,
    ModelTierConfig,
    ProcessingConfig,
    RetryModelConfig,
)
from family_assistant.llm.model_tiers import (
    resolve_claude_agent_model,
    validate_profile_model_tier,
)
from family_assistant.processing.claude_agent_loop import ClaudeAgentLoop
from family_assistant.storage.database import Database
from family_assistant.telegram.commands import normalize_slash_command
from family_assistant.telegram.service import build_profile_slash_command_map
from family_assistant.tools.on_demand import ACTIVATE_TOOLS_TOOL_NAME
from tests.mocks.fake_claude_code import FakeClaudeCode, FakeResponse, FakeText
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.config_models import ServiceProfile

PROFILE_ID = "claude_agent"


@pytest.fixture(name="agent_config")
def agent_config_fixture(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """The shipped configuration on a deployment that has a subscription token."""
    monkeypatch.setenv(CLAUDE_CODE_OAUTH_TOKEN_ENV, "test-oauth-token")
    return load_config(
        config_file_path="nonexistent-so-only-defaults.yaml", load_dotenv_file=False
    )


def _profile(config: AppConfig) -> ServiceProfile:
    return shipped_profile(config, PROFILE_ID)


def test_profile_is_left_out_without_a_subscription_token() -> None:
    profiles = [
        {"id": "claude_agent", "processing_config": {"claude_agent_config": {}}},
        {"id": "default_assistant", "processing_config": {}},
    ]

    kept = drop_unavailable_claude_agent_profiles(profiles, {})

    assert [p["id"] for p in kept] == ["default_assistant"]


def test_profile_is_kept_with_a_subscription_token() -> None:
    profiles = [
        {"id": "claude_agent", "processing_config": {"claude_agent_config": {}}}
    ]

    kept = drop_unavailable_claude_agent_profiles(
        profiles, {CLAUDE_CODE_OAUTH_TOKEN_ENV: "token"}
    )

    assert [p["id"] for p in kept] == ["claude_agent"]


def test_every_reachable_tier_is_one_claude_model(agent_config: AppConfig) -> None:
    profile = _profile(agent_config)
    validate_profile_model_tier(profile, agent_config.model_tiers)
    reachable = {
        profile.processing_config.model_tier,
        *(profile.allowed_model_tiers or ()),
        *(profile.delegation_model_tiers or ()),
    }

    models = {
        resolve_claude_agent_model(
            tier, agent_config.model_tiers[tier], agent_config.llm_parameters
        ).model
        for tier in reachable
        if tier is not None
    }

    assert models == {"claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5"}


def test_default_tier_runs_opus_at_high_effort(agent_config: AppConfig) -> None:
    model = resolve_claude_agent_model(
        "claude_opus_5_5",
        agent_config.model_tiers["claude_opus_5_5"],
        agent_config.llm_parameters,
    )

    assert (model.model, model.effort) == ("claude-opus-5-5", "high")


def test_slash_claude_reaches_the_profile(agent_config: AppConfig) -> None:
    command_map = build_profile_slash_command_map(agent_config)

    assert command_map[normalize_slash_command("/claude")] == PROFILE_ID


def test_tier_with_a_fallback_is_refused(agent_config: AppConfig) -> None:
    profile = _profile(agent_config).model_copy(
        update={"allowed_model_tiers": ["claude_opus_5_5", "deep"]}
    )

    with pytest.raises(ValueError, match="fallback"):
        validate_profile_model_tier(profile, agent_config.model_tiers)


def test_tier_on_another_provider_is_refused(agent_config: AppConfig) -> None:
    profile = _profile(agent_config).model_copy(
        update={"allowed_model_tiers": ["claude_opus_5_5", "gemini_4_argon"]}
    )

    with pytest.raises(ValueError, match="Anthropic"):
        validate_profile_model_tier(profile, agent_config.model_tiers)


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


def test_inline_model_beside_claude_agent_config_is_refused() -> None:
    config = ProcessingConfig(
        claude_agent_config=ClaudeAgentConfig(),
        provider="anthropic",
        llm_model="claude-opus-5-5",
    )

    with pytest.raises(ValueError, match="llm_model"):
        validate_claude_agent_config(PROFILE_ID, config)


def test_claude_agent_config_without_a_tier_is_refused() -> None:
    config = ProcessingConfig(claude_agent_config=ClaudeAgentConfig())

    with pytest.raises(ValueError, match="model_tier"):
        validate_claude_agent_config(PROFILE_ID, config)


@pytest.fixture(name="started")
async def started_fixture(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Assistant, FakeClaudeCode]]:
    """The shipped profile built through normal startup, on a fake CLI."""
    monkeypatch.setenv(CLAUDE_CODE_OAUTH_TOKEN_ENV, "test-oauth-token")
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

    fake = FakeClaudeCode(responses=[FakeResponse([FakeText("Done.")])])

    def client_factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
        return ClaudeSDKClient(options, transport=fake)

    assistant = Assistant(
        config=config,
        database_engine=db_engine,
        claude_agent_client_factory=client_factory,
    )
    try:
        await assistant.setup_dependencies()
        yield assistant, fake
    finally:
        await assistant.stop_services()


@pytest.mark.asyncio
async def test_startup_runs_the_profile_on_the_agent_sdk(
    started: tuple[Assistant, FakeClaudeCode],
) -> None:
    assistant, _ = started
    service = assistant.processing_services_registry[PROFILE_ID]

    assert isinstance(getattr(service, "llm_loop", None), ClaudeAgentLoop)


@pytest.mark.asyncio
async def test_session_is_offered_household_tools_without_activation(
    started: tuple[Assistant, FakeClaudeCode], db_engine: AsyncEngine
) -> None:
    assistant, fake = started
    service = assistant.processing_services_registry[PROFILE_ID]
    await service.handle_chat_interaction(
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
