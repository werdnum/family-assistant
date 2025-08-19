#!/bin/bash

# Test Verification Hook for Claude Code
# Ensures tests have been run successfully since the last file modification
# Exit codes: 0 = success/pass, 2 = blocking error (tests not run)

set -euo pipefail

# Read JSON input from stdin
INPUT=$(cat)

# Check if this is a command we want to verify tests for
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Only check for specific commands that indicate completion
if [ "$TOOL_NAME" != "Bash" ]; then
    exit 0
fi

# Check if this is one of our trigger commands
if ! echo "$COMMAND" | grep -qE "(git commit|echo done)"; then
    exit 0
fi

# Check if stop_hook_active is true to avoid loops
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    # Skip verification if we're already in a stop hook to avoid loops
    exit 0
fi

# Extract transcript path from JSON input
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')

if [ -z "$TRANSCRIPT_PATH" ]; then
    echo "Error: No transcript_path provided in input" >&2
    exit 2
fi

if [ ! -f "$TRANSCRIPT_PATH" ]; then
    echo "Error: Transcript file not found: $TRANSCRIPT_PATH" >&2
    exit 2
fi


# Find the last file modification (Edit, Write, MultiEdit) with timestamp
# Exclude modifications to docs, .claude, .devcontainer and other non-code directories
LAST_MODIFICATION=$(cat "$TRANSCRIPT_PATH" | jq -c '
    select(.type == "assistant" and .message.content) |
    .message.content[] |
    select(.type == "tool_use" and (.name == "Edit" or .name == "Write" or .name == "MultiEdit")) |
    select(
        .input.file_path and 
        (.input.file_path | test("(docs/|\\.claude/|\\.devcontainer/|.*\\.md$|.*\\.txt$|scratch/|tmp/|README)"; "i") | not)
    ) |
    {id: .id, name: .name, file: .input.file_path}
' | tail -1)

if [ -z "$LAST_MODIFICATION" ]; then
    echo "✓ No code modifications found requiring test verification"
    exit 0
fi

LAST_MOD_NAME=$(echo "$LAST_MODIFICATION" | jq -r '.name // empty')
LAST_MOD_ID=$(echo "$LAST_MODIFICATION" | jq -r '.id // empty')
LAST_MOD_FILE=$(echo "$LAST_MODIFICATION" | jq -r '.file // "unknown file"')

# Get the actual timestamp from the assistant message containing this tool
LAST_MOD_TIME=$(cat "$TRANSCRIPT_PATH" | jq -r --arg id "$LAST_MOD_ID" '
    select(.type == "assistant" and .message.content) |
    select(.message.content[] | select(.type == "tool_use" and .id == $id)) |
    .timestamp
' | head -1)

if [ -z "$LAST_MOD_TIME" ]; then
    echo "Warning: Could not determine timestamp of last modification" >&2
    # Be permissive if we can't determine the timestamp
    exit 0
fi

echo "Code modified: $LAST_MOD_FILE at $LAST_MOD_TIME" >&2

# Find all test commands after the last modification
# Look for various test command patterns
TEST_COMMANDS=$(cat "$TRANSCRIPT_PATH" | jq -c --arg mod_time "$LAST_MOD_TIME" '
    select(.type == "assistant" and .message.content and .timestamp > $mod_time) |
    .message.content[] |
    select(.type == "tool_use" and .name == "Bash") |
    select(.input.command | test("^poe\\s+test(\\s+(-[xqvs]+|--[a-z-]+|-n\\s*[0-9]+))*\\s*$"; "i")) |
    {id: .id, command: .input.command}
')

if [ -z "$TEST_COMMANDS" ]; then
    echo "❌ Tests have not been run since modifying $LAST_MOD_FILE" >&2
    echo "You MUST run 'poe test' before committing changes" >&2
    exit 2
fi

# For each test command, check if it completed successfully
SUCCESSFUL_TEST=""
while IFS= read -r test_cmd; do
    if [ -z "$test_cmd" ]; then
        continue
    fi
    
    TEST_ID=$(echo "$test_cmd" | jq -r '.id')
    TEST_COMMAND=$(echo "$test_cmd" | jq -r '.command')
    
    # Find the timestamp of this specific test command
    TEST_TIME=$(cat "$TRANSCRIPT_PATH" | jq -r --arg id "$TEST_ID" '
        select(.type == "assistant" and .message.content) |
        select(.message.content[] | select(.type == "tool_use" and .id == $id)) |
        .timestamp
    ' | head -1)
    
    # Check if this test command has a result and if it was successful
    # Look for the tool_result with matching tool_use_id
    TEST_RESULT=$(cat "$TRANSCRIPT_PATH" | jq -r --arg id "$TEST_ID" --arg test_time "$TEST_TIME" '
        select(.type == "user" and .message.content and .timestamp > $test_time) |
        .message.content[] |
        select(.type == "tool_result" and .tool_use_id == $id) |
        .is_error // false
    ' | head -1)
    
    # If TEST_RESULT is empty or "false", the test was successful
    if [ -z "$TEST_RESULT" ] || [ "$TEST_RESULT" = "false" ]; then
        SUCCESSFUL_TEST="$TEST_COMMAND"
        echo "✓ Tests passed: $TEST_COMMAND" >&2
        break
    else
        echo "⚠ Test failed: $TEST_COMMAND" >&2
    fi
done <<< "$TEST_COMMANDS"

if [ -n "$SUCCESSFUL_TEST" ]; then
    echo "✓ All required tests have passed" >&2
    exit 0
else
    echo "❌ Tests failed after modifying $LAST_MOD_FILE" >&2
    echo "You MUST fix failing tests before committing" >&2
    echo "Run 'poe test' and ensure all tests pass" >&2
    exit 2
fi

