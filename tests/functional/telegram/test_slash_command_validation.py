"""
Test validation of Telegram slash commands configured for processing profiles.

This test ensures that all slash commands defined in config.yaml meet Telegram's
requirements for bot commands:
- Must start with a forward slash (/)
- 1-32 characters after the / (33 characters total including /)
- Only lowercase English letters, numbers, and underscores
"""

import logging
import re
from pathlib import Path

import pytest
import yaml

logger = logging.getLogger(__name__)

# Telegram bot command validation pattern
# Must start with /, followed by 1-32 characters of lowercase letters, digits, and underscores
TELEGRAM_COMMAND_PATTERN = re.compile(r"^/[a-z0-9_]{1,32}$")


def load_config() -> dict:
    """Load the config.yaml file from the project root."""
    config_path = Path(__file__).parent.parent.parent.parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def extract_all_slash_commands(config: dict) -> list[tuple[str, str]]:
    """
    Extract all slash commands from all service profiles.

    Returns:
        List of tuples: (profile_id, slash_command)
    """
    slash_commands = []

    # Check default_profile_settings for slash_commands (though typically empty)
    default_settings = config.get("default_profile_settings", {})
    default_commands = default_settings.get("slash_commands", [])
    for cmd in default_commands:
        slash_commands.append(("default_profile_settings", cmd))

    # Check each service profile
    service_profiles = config.get("service_profiles", [])
    for profile in service_profiles:
        profile_id = profile.get("id", "unknown")
        profile_commands = profile.get("slash_commands", [])
        for cmd in profile_commands:
            slash_commands.append((profile_id, cmd))

    return slash_commands


def test_slash_commands_meet_telegram_requirements() -> None:
    """
    Test that all slash commands configured for processing profiles
    meet Telegram's requirements.

    Telegram requirements for bot commands:
    - Must start with /
    - 1-32 characters after the / (33 characters total including /)
    - Only lowercase English letters, numbers, and underscores
    """
    # Load config
    config = load_config()

    # Extract all slash commands
    all_commands = extract_all_slash_commands(config)

    # If no commands are configured, that's valid (though unusual)
    if not all_commands:
        logger.warning("No slash commands found in config.yaml")
        return

    # Validate each command
    invalid_commands = []
    for profile_id, command in all_commands:
        if not TELEGRAM_COMMAND_PATTERN.match(command):
            reason = []
            if not command.startswith("/"):
                reason.append("must start with /")
            elif command == "/":
                reason.append("empty command after /")
            if len(command) > 33:  # 33 = / + 32 characters
                reason.append(f"too long ({len(command)} chars, max 33 including /)")
            # Only check character validity if it has content after /
            if (
                command.startswith("/")
                and len(command) > 1
                and not re.match(r"^/[a-z0-9_]*$", command)
            ):
                reason.append(
                    "contains invalid characters (only lowercase a-z, 0-9, _ allowed)"
                )

            # If no specific reason was identified, provide a generic one
            if not reason:
                reason.append("does not match Telegram command pattern")

            invalid_commands.append({
                "profile": profile_id,
                "command": command,
                "reason": "; ".join(reason),
            })

    # Assert that all commands are valid
    if invalid_commands:
        error_msg = "Found invalid Telegram slash commands:\n"
        for invalid in invalid_commands:
            error_msg += f"  Profile '{invalid['profile']}': '{invalid['command']}' - {invalid['reason']}\n"
        error_msg += "\nTelegram requirements: /command_name with 1-32 lowercase letters, numbers, or underscores"
        pytest.fail(error_msg)

    # Log success
    logger.info(f"All {len(all_commands)} slash commands are valid for Telegram")


@pytest.mark.parametrize(
    "command,should_be_valid,reason",
    [
        # Valid commands
        ("/browse", True, "simple lowercase command"),
        ("/research", True, "lowercase with multiple letters"),
        ("/visualize", True, "longer lowercase command"),
        ("/chart", True, "short lowercase command"),
        ("/automate", True, "lowercase with 8 characters"),
        ("/test_command", True, "command with underscore"),
        ("/command123", True, "command with numbers"),
        ("/a", True, "single character command"),
        ("/a_b_c_1_2_3", True, "command with underscores and numbers"),
        ("/very_long_command_name_here", True, "long but under 32 chars"),
        # Invalid commands - too long (> 32 chars after /)
        (
            "/this_is_a_very_long_command_name_that_exceeds_the_limit",
            False,
            "exceeds 32 character limit",
        ),
        # Invalid commands - uppercase letters
        ("/Browse", False, "contains uppercase letter"),
        ("/RESEARCH", False, "all uppercase"),
        ("/myCommand", False, "camelCase"),
        # Invalid commands - special characters
        ("/test-command", False, "contains hyphen"),
        ("/test.command", False, "contains period"),
        ("/test command", False, "contains space"),
        ("/test@command", False, "contains at symbol"),
        ("/test!", False, "contains exclamation"),
        # Invalid commands - missing slash
        ("browse", False, "missing forward slash"),
        ("research", False, "missing forward slash"),
        # Invalid commands - empty or just slash
        ("/", False, "empty command after slash"),
    ],
)
def test_individual_command_validation(
    command: str, should_be_valid: bool, reason: str
) -> None:
    """
    Test individual command validation against Telegram requirements.

    This parametrized test verifies that the validation pattern correctly
    identifies both valid and invalid commands.
    """
    is_valid = bool(TELEGRAM_COMMAND_PATTERN.match(command))

    assert is_valid == should_be_valid, (
        f"Command '{command}' validation failed: {reason}. "
        f"Expected valid={should_be_valid}, got valid={is_valid}"
    )
