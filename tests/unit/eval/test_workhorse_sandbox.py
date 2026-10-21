"""The workhorse sandbox's adjustments to the shipped configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from family_assistant.config_loader import load_config
from family_assistant.config_models import RetryModelConfig
from family_assistant.eval.workhorse.google_sandbox import eval_google_integration
from family_assistant.eval.workhorse.sandbox import (
    SEAL_RULE,
    LocalServices,
    eval_tier_name,
    frozen_clock,
    sandbox_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.config_models import AppConfig

pytestmark = pytest.mark.no_db

_FLASH_LOW = RetryModelConfig(
    provider="google",
    model="gemini-3.8-flash",
    llm_parameters={"thinking_config": {"thinking_level": "low"}},
)


def _sandbox(tmp_path: Path) -> tuple[AppConfig, AppConfig]:
    shipped = load_config(
        config_file_path=str(tmp_path / "no-operator-config.yaml"),
        load_dotenv_file=False,
    )
    return shipped, sandbox_config(
        shipped,
        database_url="postgresql+asyncpg://postgres@/copy",
        port=8123,
        work_dir=tmp_path,
        user_id="andrew@example.com",
        google_integration=eval_google_integration(require_taint_enforcement=False),
        configurations={"flash_low": _FLASH_LOW},
        services=LocalServices(
            mcp_servers={
                "tuit": {
                    "url": "http://127.0.0.1:9001/mcp",
                    "transport": "streamable_http",
                }
            },
            home_assistant=("http://127.0.0.1:9002", "local-token"),
        ),
    )


def test_each_configuration_is_a_single_model_tier_on_the_profiles_under_test(
    tmp_path: Path,
) -> None:
    _, config = _sandbox(tmp_path)

    tier = config.model_tiers[eval_tier_name("flash_low")]
    assert tier.chain == [_FLASH_LOW]
    profiles = {p.id: p for p in config.service_profiles}
    for profile_id in ("default_assistant", "complex_tasks"):
        assert eval_tier_name("flash_low") in (
            profiles[profile_id].allowed_model_tiers or []
        )


def test_other_profiles_keep_their_shipped_tiers(tmp_path: Path) -> None:
    shipped, config = _sandbox(tmp_path)

    before = {p.id: p.allowed_model_tiers for p in shipped.service_profiles}
    after = {p.id: p.allowed_model_tiers for p in config.service_profiles}
    for profile_id in set(after) - {"default_assistant", "complex_tasks"}:
        assert after[profile_id] == before[profile_id]


def test_background_work_and_external_services_are_off(tmp_path: Path) -> None:
    _, config = _sandbox(tmp_path)

    assert not config.telegram_enabled
    assert config.mqtt_config.broker_host is None
    assert not config.ucp_config.enabled
    assert config.model_routing.mode == "off"
    assert not config.conversation_summaries.enabled
    # Memory is read as in production, but nothing contributes, so the
    # curator never runs.
    assert config.memory_config.enabled
    assert not any(
        p.processing_config.memory_contribute for p in config.service_profiles
    )
    assert [user.id for user in config.users] == ["andrew@example.com"]
    assert config.google_integration.oauth_client_id == "eval-client"


def test_the_clock_moves_to_the_moment_and_ticks() -> None:
    moment = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)

    with frozen_clock(moment):
        now = datetime.now(UTC)

    assert moment <= now < datetime(2026, 8, 6, 1, 1, tzinfo=UTC)


def test_profiles_that_act_outside_the_sandbox_are_removed(tmp_path: Path) -> None:
    shipped, config = _sandbox(tmp_path)

    kept = {p.id for p in config.service_profiles}
    assert {"default_assistant", "complex_tasks"} <= kept
    for profile in shipped.service_profiles:
        if "browser" in profile.id or profile.processing_config.antigravity_config:
            assert profile.id not in kept


def test_every_remaining_profile_carries_the_seal_in_the_operator_layer(
    tmp_path: Path,
) -> None:
    _, config = _sandbox(tmp_path)

    for profile in config.service_profiles:
        assert profile.operator_tools_policy is not None
        assert SEAL_RULE in profile.operator_tools_policy.rules


def test_only_locally_run_services_are_wired_and_point_locally(tmp_path: Path) -> None:
    _, config = _sandbox(tmp_path)

    assert list(config.mcp_config.mcpServers) == ["tuit"]
    assert config.mcp_config.mcpServers["tuit"].model_dump()["url"] == (
        "http://127.0.0.1:9001/mcp"
    )
    (instance,) = config.plugins.home_assistant.values()
    assert instance.api_url == "http://127.0.0.1:9002"
