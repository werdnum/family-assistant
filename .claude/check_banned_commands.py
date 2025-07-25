#!/usr/bin/env python3
"""
PreToolHook script to check Bash commands against banned patterns and enforce minimum timeouts.
Reads input from stdin and returns 0 to allow, 2 to block with message.
"""

import json
import os
import re
import sys

# Read the hook input from stdin
try:
    input_data = json.load(sys.stdin)
except json.JSONDecodeError as e:
    print(f"Error: Invalid JSON input: {e}", file=sys.stderr)
    sys.exit(1)

tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})
command = tool_input.get("command", "")
timeout = tool_input.get("timeout")

# Only check Bash tool calls with commands
if tool_name != "Bash" or not command:
    sys.exit(1)

# Load banned commands
banned_commands_path = os.path.join(os.path.dirname(__file__), "banned_commands.json")
try:
    with open(banned_commands_path) as f:
        banned_commands = json.load(f)
except (FileNotFoundError, json.JSONDecodeError) as e:
    print(f"Error loading banned commands: {e}", file=sys.stderr)
    # Allow command if we can't load the banned list
    sys.exit(0)

# Check command against banned patterns
blocked = False
for banned in banned_commands:
    pattern = banned.get("regexp", "")
    explanation = banned.get("explanation", "This command is not allowed.")

    try:
        if re.search(pattern, command):
            # Block the command and provide explanation
            print(f"• {explanation}", file=sys.stderr)
            blocked = True
            break
    except re.error:
        # If the regex is invalid, skip it
        continue

if blocked:
    # Exit code 2 blocks tool call and shows stderr to Claude
    sys.exit(2)

# Check for minimum timeout requirements
minimum_timeouts = {
    r"\bpytest\b": 300000,  # 5 minutes = 300,000 ms
    r"\bpoe\s+test\b": 900000,  # 15 minutes = 900,000 ms
}

for pattern, min_timeout_ms in minimum_timeouts.items():
    if re.search(pattern, command):
        # Convert timeout to milliseconds if it exists
        current_timeout_ms = timeout if timeout else 0

        if current_timeout_ms < min_timeout_ms:
            min_timeout_minutes = min_timeout_ms / 60000
            current_timeout_minutes = (
                current_timeout_ms / 60000 if current_timeout_ms > 0 else 0
            )

            # B005: .strip() with multi-char strings is misleading.
            # And f-string with backslash is a syntax error in older pythons.
            # So, prepare the string outside the f-string.
            cleaned_pattern = pattern.replace("\\b", "")
            print(
                f"• Command '{cleaned_pattern}' requires a minimum timeout of {min_timeout_minutes:.0f} minutes. ",
                file=sys.stderr,
            )
            if current_timeout_ms == 0:
                print(
                    f"  No timeout was specified. Please add 'timeout: {min_timeout_ms}' to your Bash tool call.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  Current timeout is {current_timeout_minutes:.1f} minutes. Please increase it to at least {min_timeout_ms}.",
                    file=sys.stderr,
                )
            sys.exit(2)

# Command is allowed
sys.exit(0)
