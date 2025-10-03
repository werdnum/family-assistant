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

    # Check if transcript was modified in the last 5 minutes (300 seconds)
    local TRANSCRIPT_AGE=$(( $(date +%s) - $(stat -c %Y "$TRANSCRIPT_PATH" 2>/dev/null || stat -f %m "$TRANSCRIPT_PATH" 2>/dev/null) ))

    if [ "$TRANSCRIPT_AGE" -gt 300 ]; then
        # Transcript is stale (>5 minutes old), use fallback strategy
        echo "DEBUG: Transcript is stale ($TRANSCRIPT_AGE seconds old), using fallback strategy" >&2

        # Check if .report.json exists
        if [ ! -f ".report.json" ]; then
            echo "❌ No test report found (.report.json missing)" >&2
            echo "You MUST run 'poe test' before committing" >&2
            return 1
        fi

        # Get .report.json timestamp
        local REPORT_TIME=$(stat -c %Y ".report.json" 2>/dev/null || stat -f %m ".report.json" 2>/dev/null)

        # Find most recently modified/deleted/renamed uncommitted file (excluding docs, .claude, etc.)
        # Use git status -z for null-terminated output to handle filenames with spaces
        local MOST_RECENT_MODIFIED=$(git status --porcelain -z | tr '\0' '\n' | grep -E '^\s*[MADR]' | while IFS= read -r line; do
            # Extract filename: strip status prefix (first 3 chars) and handle quoted names
            local file="${line:3}"
            # Remove quotes if present
            file="${file#\"}"
            file="${file%\"}"

            # Skip excluded paths
            if echo "$file" | grep -qE '(^(docs/|\.claude/|\.github/|deploy/|contrib/|scripts/review-changes\.(py|sh)|scripts/format-and-lint\.sh|\.pre-commit-config\.yaml|\.gitignore|\.dockerignore|scratch/|tmp/|README|LICENSE|CHANGELOG)|\.(md|txt)$)'; then
                continue
            fi

            if [ -f "$file" ]; then
                # File exists - use its modification time
                local FILE_TIME=$(stat -c %Y "$file" 2>/dev/null || stat -f %m "$file" 2>/dev/null)
                echo "$FILE_TIME $file"
            else
                # File was deleted - use current time to force test re-run
                echo "$(date +%s) $file (deleted)"
            fi
        done | sort -rn | head -1)

        if [ -z "$MOST_RECENT_MODIFIED" ]; then
            # No relevant uncommitted files found
            return 0
        fi

        local MOST_RECENT_TIME=$(echo "$MOST_RECENT_MODIFIED" | awk '{print $1}')
        local MOST_RECENT_FILE=$(echo "$MOST_RECENT_MODIFIED" | awk '{print $2}')

        if [ "$MOST_RECENT_TIME" -gt "$REPORT_TIME" ]; then
            local FILE_DATE=$(date -d @"$MOST_RECENT_TIME" 2>/dev/null || date -r "$MOST_RECENT_TIME" 2>/dev/null)
            local REPORT_DATE=$(date -d @"$REPORT_TIME" 2>/dev/null || date -r "$REPORT_TIME" 2>/dev/null)
            echo "❌ Tests have not been run since modifying $MOST_RECENT_FILE at $FILE_DATE" >&2
            echo "Test report is from: $REPORT_DATE" >&2
            echo "You MUST run 'poe test' before committing" >&2
            return 1
        fi

        # Check if there are any failed tests in the report
        local FAILED_COUNT=$(jq '.summary.failed // 0' .report.json 2>/dev/null)
        if [ "$FAILED_COUNT" -gt 0 ]; then
            echo "❌ Test report shows $FAILED_COUNT failed test(s)" >&2
            echo "You MUST fix all failing tests before committing" >&2
            echo "The acceptable number of test failures is zero." >&2
            return 1
        fi

        # Tests are recent enough and all passing
        return 0
    fi

    echo "DEBUG: Using transcript path: $TRANSCRIPT_PATH" >&2

    # Find the last file modification (Edit, Write, MultiEdit) with timestamp
    # Exclude modifications to files that don't affect poe test results
    local LAST_MODIFICATION=$(cat "$TRANSCRIPT_PATH" | jq -c '
        select(.type == "assistant" and .message.content and (.message.content | type == "array")) |
        .message.content[] |
        select(.type == "tool_use" and (.name == "Edit" or .name == "Write" or .name == "MultiEdit")) |
        select(
            .input.file_path and
            (.input.file_path | test("(
                docs/|
                \\.claude/|
                \\.github/|
                \\.devcontainer/|
                deploy/|
                contrib/|
                scripts/review-changes\\.(py|sh)|
                scripts/format-and-lint\\.sh|
                \\.pre-commit-config\\.yaml|
                \\.gitignore|
                \\.dockerignore|
                \\.(md|txt)$|
                scratch/|
                tmp/|
                README|
                LICENSE|
                CHANGELOG
            )"; "ix") | not)
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
        echo "❌ Tests have not been run since modifying $LAST_MOD_FILE at $LAST_MOD_TIME" >&2
        echo "You MUST run 'poe test' before finishing" >&2
        echo "Reminder: only 'poe test' (with optional -xq) will do. No other commands will satisfy this hook." >&2
        echo "... even if you think these changes don't impact any or all tests." >&2
        echo "If you have other feedback to address, do that first – you will need to re-run tests after making any further changes." >&2
        return 1
    fi

    # Check if any test command completed successfully
    local SUCCESSFUL_TEST=""
    local LAST_TEST_ATTEMPT_COMMAND=""
    local LAST_TEST_ATTEMPT_TIME=""
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

        LAST_TEST_ATTEMPT_COMMAND="$TEST_COMMAND"
        LAST_TEST_ATTEMPT_TIME="$TEST_TIME"

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
        echo "❌ Tests failed after modifying $LAST_MOD_FILE at $LAST_MOD_TIME" >&2
        echo "You MUST fix failing tests before finishing" >&2
        echo "Reminder: commits are not accepted without a passing run of poe test." >&2
        echo "This means that there were NO test failures before you started, there are no pre-existing issues." >&2
        echo "NO EXCUSES! Even if you've made 'substantial progress'." >&2
        echo "If you believe the failure to be a flake, prove it by rerunning poe test to get a passing result." >&2
        echo "If you have other feedback to address, do that first – you will need to re-run tests after making any further changes." >&2
        echo "If you are stuck, consider using @agent-systematic-debugger with a detailed prompt explaining what changes you have made, what you have tried so far and any findings." >&2
        if [ -n "$LAST_TEST_ATTEMPT_COMMAND" ]; then
            if [ -n "$LAST_TEST_ATTEMPT_TIME" ]; then
                echo "Last failing test command: '$LAST_TEST_ATTEMPT_COMMAND' at $LAST_TEST_ATTEMPT_TIME" >&2
            else
                echo "Last failing test command: '$LAST_TEST_ATTEMPT_COMMAND' (timestamp unavailable)" >&2
            fi
        fi
        return 1
    fi
}

