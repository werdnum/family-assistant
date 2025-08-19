#!/bin/bash

# Core test verification logic for Claude Code hooks
# Returns 0 if tests are passing, 1 if tests need attention

check_test_status() {
    local TRANSCRIPT_PATH="$1"
    
    if [ -z "$TRANSCRIPT_PATH" ]; then
        echo "Error: No transcript_path provided" >&2
        return 1
    fi

    if [ ! -f "$TRANSCRIPT_PATH" ]; then
        echo "Error: Transcript file not found: $TRANSCRIPT_PATH" >&2
        return 1
    fi

    # Find the last file modification (Edit, Write, MultiEdit) with timestamp
    # Exclude modifications to docs, .claude, .devcontainer and other non-code directories
    local LAST_MODIFICATION=$(cat "$TRANSCRIPT_PATH" | jq -c '
        select(.type == "assistant" and .message.content and (.message.content | type == "array")) |
        .message.content[] |
        select(.type == "tool_use" and (.name == "Edit" or .name == "Write" or .name == "MultiEdit")) |
        select(
            .input.file_path and 
            (.input.file_path | test("(docs/|\\.claude/|\\.devcontainer/|\\.(md|txt)$|scratch/|tmp/|README)"; "i") | not)
        ) |
        {id: .id, name: .name, file: .input.file_path}
    ' 2>/dev/null | tail -1)

    if [ -z "$LAST_MODIFICATION" ]; then
        # No code modifications found - tests are not required
        return 0
    fi

    local LAST_MOD_NAME=$(echo "$LAST_MODIFICATION" | jq -r '.name // empty')
    local LAST_MOD_ID=$(echo "$LAST_MODIFICATION" | jq -r '.id // empty')
    local LAST_MOD_FILE=$(echo "$LAST_MODIFICATION" | jq -r '.file // "unknown file"')

    # Get the actual timestamp from the assistant message containing this tool
    local LAST_MOD_TIME=$(cat "$TRANSCRIPT_PATH" | jq -r --arg id "$LAST_MOD_ID" '
        select(.type == "assistant" and .message.content) |
        select(.message.content[] | select(.type == "tool_use" and .id == $id)) |
        .timestamp
    ' | head -1)

    if [ -z "$LAST_MOD_TIME" ]; then
        echo "Warning: Could not determine timestamp of last modification" >&2
        # Be permissive if we can't determine the timestamp
        return 0
    fi

    # Find all test commands after the last modification
    local TEST_COMMANDS=$(cat "$TRANSCRIPT_PATH" | jq -c --arg mod_time "$LAST_MOD_TIME" '
        select(.type == "assistant" and .message.content and (.message.content | type == "array") and .timestamp > $mod_time) |
        .message.content[] |
        select(.type == "tool_use" and .name == "Bash") |
        select(.input.command | test("^poe\\s+test(\\s+(-[xqvs]+|--[a-z-]+|-n\\s*[0-9]+))*\\s*$"; "i")) |
        {id: .id, command: .input.command}
    ' 2>/dev/null)

    if [ -z "$TEST_COMMANDS" ]; then
        echo "❌ Tests have not been run since modifying $LAST_MOD_FILE" >&2
        echo "You MUST run 'poe test' before finishing" >&2
        return 1
    fi

    # Check if any test command completed successfully
    local SUCCESSFUL_TEST=""
    while IFS= read -r test_cmd; do
        if [ -z "$test_cmd" ]; then
            continue
        fi
        
        local TEST_ID=$(echo "$test_cmd" | jq -r '.id')
        local TEST_COMMAND=$(echo "$test_cmd" | jq -r '.command')
        
        # Find the timestamp of this specific test command
        local TEST_TIME=$(cat "$TRANSCRIPT_PATH" | jq -r --arg id "$TEST_ID" '
            select(.type == "assistant" and .message.content) |
            select(.message.content[] | select(.type == "tool_use" and .id == $id)) |
            .timestamp
        ' | head -1)
        
        # Check if this test command has a result and if it was successful
        local TEST_RESULT=$(cat "$TRANSCRIPT_PATH" | jq -r --arg id "$TEST_ID" --arg test_time "$TEST_TIME" '
            select(.type == "user" and .message.content and (.message.content | type == "array") and .timestamp > $test_time) |
            .message.content[] |
            select(.type == "tool_result" and .tool_use_id == $id) |
            .is_error // false
        ' 2>/dev/null | head -1)
        
        # If TEST_RESULT is empty or "false", the test was successful
        if [ -z "$TEST_RESULT" ] || [ "$TEST_RESULT" = "false" ]; then
            SUCCESSFUL_TEST="$TEST_COMMAND"
            break
        fi
    done <<< "$TEST_COMMANDS"

    if [ -n "$SUCCESSFUL_TEST" ]; then
        return 0  # Tests are passing
    else
        echo "❌ Tests failed after modifying $LAST_MOD_FILE" >&2
        echo "You MUST fix failing tests before finishing" >&2
        return 1
    fi
}

