#!/usr/bin/env python3
"""
PreToolHook script to check Bash commands against banned patterns.
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

# Command is allowed
sys.exit(0)
