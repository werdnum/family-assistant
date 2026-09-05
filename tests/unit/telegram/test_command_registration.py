"""What Telegram is told about the commands it offers.

The bot menu and the dispatch maps are built from configuration rather than
written out in the handler, so these pin the shipped result: the tier commands
reach the menu with a description a reader can act on, they resolve to the
tiers they name, and a word two things claim is refused rather than silently
answered by whichever was registered first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.config_loader import load_config
from family_assistant.telegram.service import (
    build_bot_commands,
    build_profile_slash_command_map,
    build_tier_slash_command_map,
)

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig


@pytest.fixture(name="shipped_config")
def shipped_config_fixture() -> AppConfig:
    """The application configuration with no operator `config.yaml` applied."""
    return load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )


def test_the_shipped_tier_commands_dispatch_to_their_tiers(
    shipped_config: AppConfig,
) -> None:
    profile_commands = build_profile_slash_command_map(shipped_config)

    tier_commands = build_tier_slash_command_map(shipped_config, profile_commands)

    assert tier_commands == {"/deep": "deep", "/max": "frontier"}


def test_the_tier_commands_reach_the_bot_menu_with_a_description(
    shipped_config: AppConfig,
) -> None:
    """A tier command is worth nothing if a user cannot find it."""
    commands = {
        command.command: command.description
        for command in build_bot_commands(shipped_config)
    }

    assert "deep" in commands
    assert "max" in commands
    assert commands["deep"].startswith("Deep")
    assert commands["max"].startswith("Max")
    assert "reasoning" in commands["deep"] or "judgement" in commands["deep"]


def test_the_menu_still_offers_the_profile_commands(
    shipped_config: AppConfig,
) -> None:
    """Tier commands are added beside the profile commands, not instead."""
    commands = {command.command for command in build_bot_commands(shipped_config)}

    assert {"start", "interrupt", "complex", "engineer"} <= commands


def test_a_tier_claiming_a_profile_command_is_refused(
    shipped_config: AppConfig,
) -> None:
    """Startup validation already refuses this; so does the dispatch map.

    Two things answering one word means the lookup picks one and the other
    silently never runs, so the map says which pair collided rather than
    choosing between them.
    """
    with pytest.raises(ValueError, match="deep") as refusal:
        build_tier_slash_command_map(shipped_config, {"/deep": "some_profile"})

    assert "some_profile" in str(refusal.value)
