"""What Telegram is told about the commands it offers.

The bot menu and the dispatch maps are built from configuration rather than
written out in the handler, so these pin the shipped result: the tier commands
reach the menu with a description a reader can act on, and they resolve to the
tiers they name. A word two things claim is refused by `AppConfig`, which is
covered where that validator lives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.telegram.service import (
    build_bot_commands,
    build_tier_slash_command_map,
)

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig


def test_the_shipped_tier_commands_dispatch_to_their_tiers(
    shipped_config: AppConfig,
) -> None:
    tier_commands = build_tier_slash_command_map(shipped_config)

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
