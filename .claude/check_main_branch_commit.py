#!/usr/bin/env python3
"""
PreToolHook script to prevent commits on the main branch.
Reads input from stdin and returns 0 to allow, 2 to block with message.
"""

import json
import os
import re
import subprocess
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

# Only check Bash tool calls with git commit commands
if tool_name != "Bash" or not command:
    sys.exit(0)

# Check if this is a git commit command (but not with --no-verify)
if not re.search(r"\bgit\s+commit\b", command) or "--no-verify" in command:
    sys.exit(0)

# Get the current branch
result = subprocess.run(
    ["git", "branch", "--show-current"], capture_output=True, text=True, cwd=os.getcwd()
)

# Check if the command succeeded
if result.returncode != 0:
    # If we can't determine the branch, allow the commit
    print(
        f"Warning: Could not determine current branch: {result.stderr}", file=sys.stderr
    )
    sys.exit(0)

current_branch = result.stdout.strip()

# Check if we're on the main branch
if current_branch in ["main", "master"]:
    print(
        f"• You're currently on the '{current_branch}' branch. Direct commits to the main branch are not allowed.",
        file=sys.stderr,
    )
    print(
        "• Please create a feature branch first: git checkout -b feature-name",
        file=sys.stderr,
    )
    print(
        "• Then you can commit your changes and create a pull request.", file=sys.stderr
    )
    sys.exit(2)

# Allow commits on feature branches
sys.exit(0)
