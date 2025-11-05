#!/usr/bin/env python3
"""
PreToolUse hook script to check Bash commands against configuration rules.
Uses updatedInput feature to auto-fix commands where possible.

Exit codes:
- 0: Allow (with optional updatedInput modifications via stdout JSON)
- 2: Block (with explanation via stderr)
"""

import json
import os
import re
import sys


def load_config() -> dict:
    """Load and parse the banned_commands.json configuration file."""
    config_path = os.path.join(os.path.dirname(__file__), "banned_commands.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        sys.exit(1)


def check_command_rules(
    command: str, rules: list
) -> tuple[str | None, str | None, str]:
    """
    Check command against rules and return action to take.

    Returns:
        tuple of (action, replacement, explanation)
        - action: "block", "replace", or None
        - replacement: replacement string if action is "replace"
        - explanation: human-readable explanation
    """
    for rule in rules:
        pattern = rule.get("regexp", "")
        action = rule.get("action", "block")
        explanation = rule.get("explanation", "This command is not allowed.")

        try:
            match = re.search(pattern, command)
            if match:
                replacement = None
                if action == "replace":
                    replacement_template = rule.get("replacement", "")
                    # Perform regex substitution with capture groups
                    replacement = re.sub(pattern, replacement_template, command)

                return action, replacement, explanation
        except re.error:
            # If the regex is invalid, skip it
            continue

    return None, None, ""


def check_timeout_requirements(
    command: str,
    current_timeout: int | None,
    requirements: list,
    default_timeout_ms: int,
) -> tuple[int | None, str]:
    """
    Check if command needs a minimum timeout and return required timeout if needed.

    Returns:
        tuple of (required_timeout_ms, explanation)
        - required_timeout_ms: minimum timeout needed, or None if current is sufficient
        - explanation: human-readable explanation
    """
    for requirement in requirements:
        pattern = requirement.get("regexp", "")
        min_timeout_ms = requirement.get("minimum_timeout_ms", 0)
        explanation = requirement.get(
            "explanation", "This command requires a longer timeout."
        )

        try:
            if re.search(pattern, command):
                # Use default if no timeout specified
                effective_timeout = (
                    current_timeout
                    if current_timeout is not None
                    else default_timeout_ms
                )

                if effective_timeout < min_timeout_ms:
                    return min_timeout_ms, explanation
                return None, ""
        except re.error:
            continue

    return None, ""


def check_background_restrictions(
    command: str, run_in_background: bool, restrictions: list
) -> tuple[bool, str]:
    """
    Check if command should not run in background.

    Returns:
        tuple of (should_fix, explanation)
        - should_fix: True if run_in_background should be set to False
        - explanation: human-readable explanation
    """
    if not run_in_background:
        # Already correct, no fix needed
        return False, ""

    for restriction in restrictions:
        pattern = restriction.get("regexp", "")
        explanation = restriction.get(
            "explanation", "This command must run in foreground."
        )

        try:
            if re.search(pattern, command):
                return True, explanation
        except re.error:
            continue

    return False, ""


def main() -> None:
    """Main entry point for the hook."""
    # Read the hook input from stdin
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    # Only check Bash tool calls with commands
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")

    if tool_name != "Bash" or not command:
        sys.exit(0)

    # Load configuration
    config = load_config()

    # Track modifications to apply
    updated_input = {}
    modifications = []

    # Check command rules (block or replace)
    action, replacement, explanation = check_command_rules(
        command, config.get("command_rules", [])
    )

    if action == "block":
        # Block the command
        print(f"• {explanation}", file=sys.stderr)
        sys.exit(2)

    if action == "replace" and replacement and replacement != command:
        # Apply command replacement
        updated_input["command"] = replacement
        modifications.append(f"Rewriting command: '{command}' → '{replacement}'")
        modifications.append(f"  Reason: {explanation}")
        # Use the replacement for subsequent checks
        command = replacement

    # Check timeout requirements
    current_timeout = tool_input.get("timeout")
    required_timeout, timeout_explanation = check_timeout_requirements(
        command,
        current_timeout,
        config.get("timeout_requirements", []),
        config.get("default_timeout_ms", 120000),
    )

    if required_timeout is not None:
        updated_input["timeout"] = required_timeout
        timeout_minutes = required_timeout / 60000
        if current_timeout is None:
            modifications.append(
                f"Auto-setting timeout to {timeout_minutes:.0f} minutes for this command"
            )
        else:
            current_minutes = current_timeout / 60000
            modifications.append(
                f"Auto-increasing timeout from {current_minutes:.1f} to {timeout_minutes:.0f} minutes"
            )
        modifications.append(f"  Reason: {timeout_explanation}")

    # Check background restrictions
    run_in_background = tool_input.get("run_in_background", False)
    should_fix_background, background_explanation = check_background_restrictions(
        command, run_in_background, config.get("background_restrictions", [])
    )

    if should_fix_background:
        updated_input["run_in_background"] = False
        modifications.append("Auto-changing to run in foreground (not background)")
        modifications.append(f"  Reason: {background_explanation}")

    # If we have modifications, output them
    if updated_input:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "\n".join(modifications),
                "updatedInput": updated_input,
            }
        }
        print(json.dumps(output, indent=2))
        sys.exit(0)

    # No changes needed, allow as-is
    sys.exit(0)


if __name__ == "__main__":
    main()
