#!/bin/bash

# This hook runs formatters, linters, and code review before git commits
# It reads JSON input from stdin and implements an improved workflow:
# 1. Parse and execute git add commands
# 2. Stash unstaged changes to isolate what's being committed
# 3. Apply formatters/linters and stage their changes
# 4. Loop on pre-commit hooks, staging any changes they make
# 5. Review the final staged changes
# 6. Commit if approved, or restore stashed changes if not

# Redirect ALL stdout to stderr by default (fail-safe for clean JSON output)
# This ensures only explicit JSON responses go to stdout, preventing contamination
exec 3>&1          # Save original stdout as file descriptor 3
exec 1>&2          # Redirect all stdout to stderr

# Temporary file to store JSON output (using mktemp for security)
JSON_OUTPUT_FILE=$(mktemp)

# Function to capture JSON for final output
output_json() {
    cat > "$JSON_OUTPUT_FILE"
}

# Color codes (removed for LLM readability)
RED=''
GREEN=''
YELLOW=''
BLUE=''
CYAN=''
BOLD=''
NC='' # No Color

# Global state
STASHED_CHANGES=false
STASH_REF=""
REPO_ROOT=""

# Function to output final JSON to stdout at the very end
final_json_output() {
    if [[ -f "$JSON_OUTPUT_FILE" ]]; then
        # Output JSON to saved stdout file descriptor 3
        cat "$JSON_OUTPUT_FILE" >&3
        rm -f "$JSON_OUTPUT_FILE" # Clean up temp file
    fi
}

# Cleanup function
cleanup() {
    if [[ "$STASHED_CHANGES" == "true" ]] && [[ -n "$STASH_REF" ]]; then
        echo "" >&2
        echo "${YELLOW}Restoring stashed changes...${NC}" >&2
        
        # Try to restore stashed changes using the specific stash reference
        if git stash apply --quiet "$STASH_REF" 2>/dev/null; then
            # Successfully applied, now drop the stash
            git stash drop --quiet "$STASH_REF" 2>/dev/null || true
            echo "${GREEN}✅ Stashed changes restored successfully${NC}" >&2
        else
            # If stash pop fails due to conflicts, force restore the stashed changes
            echo "${YELLOW}Stash conflict detected - force-restoring pre-hook state...${NC}" >&2
            echo "${YELLOW}(Working directory changes may be overwritten for conflicting files)${NC}" >&2
            
            # Get the stash content and apply it using the specific reference
            if git checkout "$STASH_REF" -- . 2>/dev/null; then
                echo "${GREEN}✅ Working changes restored from stash${NC}" >&2
                echo "${YELLOW}Note: Stash preserved at $STASH_REF for safety${NC}" >&2
                echo "${YELLOW}You can manually drop it with: git stash drop${NC}" >&2
            else
                echo "${RED}❌ Failed to restore stashed changes${NC}" >&2
                echo "${YELLOW}Your changes are still safe in the stash${NC}" >&2
                echo "${YELLOW}Manual intervention required: try 'git stash pop' after resolving conflicts${NC}" >&2
            fi
        fi
    fi
}

# Set up trap to run cleanup first, then output JSON
cleanup_and_output() {
    cleanup
    final_json_output
}

# Set trap for cleanup and final JSON output
trap cleanup_and_output EXIT

# Read JSON input from stdin
JSON_INPUT=$(cat)

# Extract the bash command from the JSON input
# For a Bash tool call, tool_input.command contains the command
COMMAND=$(echo "$JSON_INPUT" | jq -r '.tool_input.command // ""')

# Check if this is a git commit command or PR creation
# This regex matches:
# - Simple: git commit -m "message"
# - Compound: git add . && git commit -m "message"
# - With semicolon: git add .; git commit -m "message"
# - With env vars: SOME_VAR=value git commit -m "message"
# - PR creation: gh pr create
# Look for git commit/ci or gh pr create anywhere in the command
if ! echo "$COMMAND" | grep -qE "(git\s+(commit|ci)|gh\s+pr\s+create)(\s|$)"; then
    # Not a git commit or PR creation, allow it
    exit 0
fi

# Get the repository root (already declared as global)
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [[ -z "$REPO_ROOT" ]]; then
    # Not in a git repo, allow the command
    exit 0
fi

# Detect if this is a PR creation
IS_PR_CREATE=false
if echo "$COMMAND" | grep -qE "gh\s+pr\s+create"; then
    IS_PR_CREATE=true
fi

echo "${BLUE}🔍 Running improved pre-commit review workflow...${NC}" >&2
echo "" >&2

# Step 1: Parse and execute git add commands from compound commands
HAS_GIT_ADD=false
if echo "$COMMAND" | grep -qE "(^|[;&|])\s*git\s+add\s+"; then
    HAS_GIT_ADD=true
    echo "${CYAN}Step 1: Parsing and executing git add commands...${NC}" >&2
    
    # Extract git add commands - handle multiple patterns
    # This handles: "git add file1 file2 && git commit" or "git add .; git commit" etc.
    ADD_COMMANDS=$(echo "$COMMAND" | tr '\n' ' ' | grep -oE "git\s+add\s+[^;&|]*" || true)
    
    if [[ -n "$ADD_COMMANDS" ]]; then
        while IFS= read -r add_cmd; do
            if [[ -n "$add_cmd" ]]; then
                echo "${CYAN}Running: $add_cmd${NC}" >&2
                if ! eval "$add_cmd" 2>&1; then
                    echo "${RED}❌ git add failed${NC}" >&2
                    exit 0
                fi
            fi
        done <<< "$ADD_COMMANDS"
        echo "${GREEN}✅ Git add commands completed${NC}" >&2
    else
        echo "${YELLOW}⚠️  No git add commands found to execute${NC}" >&2
    fi
    echo "" >&2
fi

# Step 2: Check if we have staged changes to work with
if ! git diff --cached --quiet; then
    echo "${CYAN}Step 2: Isolating staged changes with git stash...${NC}" >&2
    
    # Stash unstaged changes, keeping only staged changes in working directory
    if ! git diff --quiet; then
        echo "${CYAN}Stashing unstaged changes to isolate committed changes...${NC}" >&2
        # Create a stash and get its unique reference
        STASH_REF=$(git stash create "review-hook: unstaged changes $(date +%s)")
        if [[ -n "$STASH_REF" ]]; then
            # Save the stash with the reference we got
            git stash store -m "review-hook: unstaged changes $(date +%s)" "$STASH_REF"
            # Reset to match the index (keeps staged changes, removes unstaged)
            git checkout-index -a -f
            STASHED_CHANGES=true
            echo "${GREEN}✅ Unstaged changes stashed (ref: ${STASH_REF:0:8})${NC}" >&2
        else
            echo "${YELLOW}⚠️  Failed to create stash, proceeding anyway${NC}" >&2
        fi
    else
        echo "${GREEN}✅ No unstaged changes to stash${NC}" >&2
    fi
    echo "" >&2
else
    if [[ "$HAS_GIT_ADD" == "true" ]]; then
        echo "${YELLOW}⚠️  No changes were staged by git add commands${NC}" >&2
        echo "" >&2
        exit 0
    fi
fi

# Step 3: Run formatters and linters on staged changes
echo "${CYAN}Step 3: Running formatters and linters on staged changes...${NC}" >&2

# Run format and lint script if available
if [[ -f "$REPO_ROOT/scripts/format-and-lint.sh" ]]; then
    # Get list of staged files for targeted formatting
    STAGED_FILES=()
    while IFS= read -r -d '' file; do
        STAGED_FILES+=("$file")
    done < <(git diff --cached --name-only -z)
    
    if [[ ${#STAGED_FILES[@]} -gt 0 ]]; then
        echo "${CYAN}Running format-and-lint.sh on staged files...${NC}" >&2
        
        # Run the formatting script on staged files
        if "$REPO_ROOT/scripts/format-and-lint.sh" "${STAGED_FILES[@]}" 2>&1; then
            echo "${GREEN}✅ Formatting and linting completed${NC}" >&2
            
            # Stage any changes made by formatters/linters
            if ! git diff --quiet; then
                echo "${CYAN}Staging changes made by formatters/linters...${NC}" >&2
                git add "${STAGED_FILES[@]}"
                echo "${GREEN}✅ Format/lint changes staged${NC}" >&2
            fi
        else
            echo "${RED}❌ Formatting/linting failed${NC}" >&2
            echo "Please fix the issues before committing." >&2
            exit 0
        fi
    else
        echo "${YELLOW}⚠️  No staged files to format/lint${NC}" >&2
    fi
else
    echo "${YELLOW}⚠️  scripts/format-and-lint.sh not found, trying poe commands...${NC}" >&2
    
    # Fallback to poe commands
    if [[ -f "$REPO_ROOT/.venv/bin/poe" ]]; then
        FORMATTER_OUTPUT=$("$REPO_ROOT/.venv/bin/poe" format 2>&1)
        FORMAT_EXIT=$?
        
        if [[ $FORMAT_EXIT -ne 0 ]]; then
            echo "${RED}❌ Formatter failed${NC}" >&2
            echo "$FORMATTER_OUTPUT" >&2
            echo "" >&2
            echo "Please fix formatting issues before committing." >&2
            exit 0
        fi
        
        LINTER_OUTPUT=$("$REPO_ROOT/.venv/bin/poe" lint-fast 2>&1)
        LINT_EXIT=$?
        
        if [[ $LINT_EXIT -ne 0 ]]; then
            echo "${RED}❌ Linter failed${NC}" >&2
            echo "$LINTER_OUTPUT" >&2
            echo "" >&2
            echo "Please fix linting issues before committing." >&2
            exit 0
        fi
        
        # Stage any changes made by formatters
        if ! git diff --quiet; then
            echo "${CYAN}Staging changes made by formatters/linters...${NC}" >&2
            git add -u
            echo "${GREEN}✅ Format/lint changes staged${NC}" >&2
        fi
        
        echo "${GREEN}✅ Formatting and linting passed${NC}" >&2
    else
        echo "${YELLOW}⚠️  No formatting tools found, skipping format/lint${NC}" >&2
    fi
fi

echo "" >&2

# Step 4: Run pre-commit hooks in a loop, staging any changes they make
echo "${CYAN}Step 4: Running pre-commit hooks with change detection...${NC}" >&2

if command -v pre-commit &> /dev/null && [[ -f "$REPO_ROOT/.pre-commit-config.yaml" ]]; then
    MAX_PRECOMMIT_ITERATIONS=5
    iteration=0
    
    while [[ $iteration -lt $MAX_PRECOMMIT_ITERATIONS ]]; do
        iteration=$((iteration + 1))
        echo "${CYAN}Pre-commit iteration $iteration...${NC}" >&2
        
        # Run pre-commit on staged files (safe for filenames with spaces)
        STAGED_FILES_FOR_PRECOMMIT=()
        while IFS= read -r -d '' file; do
            STAGED_FILES_FOR_PRECOMMIT+=("$file")
        done < <(git diff --cached --name-only -z)
        
        if [[ ${#STAGED_FILES_FOR_PRECOMMIT[@]} -eq 0 ]]; then
            echo "${YELLOW}No staged files for pre-commit hooks${NC}" >&2
            break
        fi
        
        # Capture pre-commit output to show on failure
        PRECOMMIT_OUTPUT=$(pre-commit run --files "${STAGED_FILES_FOR_PRECOMMIT[@]}" 2>&1)
        PRECOMMIT_EXIT=$?
        
        if [[ $PRECOMMIT_EXIT -eq 0 ]]; then
            # Pre-commit passed, check if it made any changes
            if git diff --quiet; then
                echo "${GREEN}✅ Pre-commit hooks completed (no changes made)${NC}" >&2
                break
            else
                echo "${YELLOW}Pre-commit hooks made changes, staging them...${NC}" >&2
                git add -u
                echo "${GREEN}✅ Pre-commit changes staged${NC}" >&2
                
                if [[ $iteration -eq $MAX_PRECOMMIT_ITERATIONS ]]; then
                    echo "${YELLOW}⚠️  Reached maximum pre-commit iterations ($MAX_PRECOMMIT_ITERATIONS)${NC}" >&2
                    echo "${YELLOW}Proceeding with current staged changes${NC}" >&2
                    break
                fi
            fi
        else
            echo "${RED}❌ Pre-commit hooks failed${NC}" >&2
            echo "" >&2
            echo "${BOLD}Pre-commit output:${NC}" >&2
            echo "$PRECOMMIT_OUTPUT" >&2
            echo "" >&2
            echo "${YELLOW}Please fix the issues and try again.${NC}" >&2
            exit 0
        fi
    done
else
    echo "${YELLOW}⚠️  Pre-commit not available or not configured, skipping${NC}" >&2
fi

echo "" >&2

# Step 5: Run code review on final staged changes
echo "${CYAN}Step 5: Running code review on final staged changes...${NC}" >&2

# Check if review script exists
REVIEW_SCRIPT="$REPO_ROOT/scripts/review-changes.sh"
if [[ ! -x "$REVIEW_SCRIPT" ]]; then
    # Review script doesn't exist or isn't executable
    echo "${YELLOW}Review script not found, skipping code review${NC}" >&2
    exit 0
fi

# Get current HEAD commit hash
HEAD_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "no-head")

# Check for sentinel phrase or bypass pattern in the entire command
# The sentinel should be: "Reviewed: HEAD-<commit-hash>" or "Bypass-Review: <reason>"
SENTINEL_PHRASE="Reviewed: HEAD-$HEAD_COMMIT"
HAS_REVIEWED=false
HAS_BYPASS=false
BYPASS_REASON=""

# Check for the original sentinel phrase
if echo "$COMMAND" | grep -qF "$SENTINEL_PHRASE"; then
    HAS_REVIEWED=true
    echo "${GREEN}✅ Found review acknowledgment: $SENTINEL_PHRASE${NC}" >&2
    echo "" >&2
fi

# Check for the bypass pattern
if echo "$COMMAND" | grep -qE "Bypass-Review:[[:space:]]*([^\"\'$'\n']+)"; then
    # Extract the bypass reason
    BYPASS_REASON=$(echo "$COMMAND" | sed -n "s/.*Bypass-Review:[[:space:]]*\([^\"\'$'\n']*\).*/\1/p" | head -1 | xargs)
    if [[ -n "$BYPASS_REASON" && "$BYPASS_REASON" != "<reason>" && "$BYPASS_REASON" != "reason" ]]; then
        HAS_BYPASS=true
        echo "${YELLOW}⚠️  Found bypass request: Bypass-Review: $BYPASS_REASON${NC}" >&2
        echo "" >&2
    else
        echo "${RED}❌ Invalid bypass format: Bypass-Review requires an actual reason${NC}" >&2
        echo "${YELLOW}Example: Bypass-Review: This is safe because...${NC}" >&2
        echo "" >&2
    fi
fi

# For PR creation, extract the body text to check for sentinel
if [[ "$IS_PR_CREATE" == "true" ]]; then
    # Extract PR body from --body flag
    PR_BODY=""
    if [[ "$COMMAND" =~ --body[[:space:]]*[\"\']([^\"\']+)[\"\'] ]]; then
        PR_BODY="${BASH_REMATCH[1]}"
    elif [[ "$COMMAND" =~ --body[[:space:]]*=[[:space:]]*[\"\']([^\"\']+)[\"\'] ]]; then
        PR_BODY="${BASH_REMATCH[1]}"
    fi
    
    if [[ -n "$PR_BODY" ]]; then
        if echo "$PR_BODY" | grep -qF "$SENTINEL_PHRASE"; then
            HAS_REVIEWED=true
            echo "${GREEN}✅ Found review acknowledgment in PR body: $SENTINEL_PHRASE${NC}" >&2
            echo "" >&2
        elif echo "$PR_BODY" | grep -qE "Bypass-Review:[[:space:]]*([^\"\'$'\n']+)"; then
            BYPASS_REASON=$(echo "$PR_BODY" | sed -n "s/.*Bypass-Review:[[:space:]]*\([^\"\'$'\n']*\).*/\1/p" | head -1 | xargs)
            if [[ -n "$BYPASS_REASON" && "$BYPASS_REASON" != "<reason>" && "$BYPASS_REASON" != "reason" ]]; then
                HAS_BYPASS=true
                echo "${YELLOW}⚠️  Found bypass request in PR body: Bypass-Review: $BYPASS_REASON${NC}" >&2
                echo "" >&2
            else
                echo "${RED}❌ Invalid bypass format in PR body: Bypass-Review requires an actual reason${NC}" >&2
                echo "${YELLOW}Use: Bypass-Review: <your actual reason why this is safe>${NC}" >&2
                echo "" >&2
            fi
        fi
    fi
fi

# Run the review script and capture output
echo "${CYAN}Analyzing staged changes for issues...${NC}" >&2
echo "" >&2

# Run the review script on staged changes
REVIEW_OUTPUT=$("$REVIEW_SCRIPT" 2>&1)
REVIEW_EXIT_CODE=$?

# Echo the captured output to stderr
echo "$REVIEW_OUTPUT" >&2

# Step 6: Process review results and decide whether to proceed
echo "" >&2

# Handle Bypass-Review: requires user approval for ANY issues
if [[ "$HAS_BYPASS" == "true" ]]; then
    if [[ $REVIEW_EXIT_CODE -eq 2 ]]; then
        echo "${YELLOW}⚠️ Code review found MAJOR issues${NC}" >&2
        echo "${RED}WARNING: Bypassing serious issues:${NC}" >&2
        echo "• Potential build-breaking changes" >&2
        echo "• Possible runtime errors" >&2
        echo "• Security risks" >&2
        echo "" >&2
        echo "${YELLOW}Bypass reason: $BYPASS_REASON${NC}" >&2
        echo "${YELLOW}⚠️  Requesting user approval to bypass MAJOR issues...${NC}" >&2
        
        # Return JSON for hooks - ask for user approval
        output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": "MAJOR issues found. Bypass requested: $BYPASS_REASON\n\nThese are serious issues that could break the build or cause runtime errors. Do you want to proceed anyway?"
  }
}
EOF
        exit 0
    elif [[ $REVIEW_EXIT_CODE -eq 1 ]]; then
        echo "${YELLOW}⚠️  Minor issues found${NC}" >&2
        echo "${YELLOW}Bypass reason: $BYPASS_REASON${NC}" >&2
        echo "${YELLOW}⚠️  Requesting user approval to bypass...${NC}" >&2
        
        # Return JSON for hooks - ask for user approval
        output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": "Minor issues found. Bypass requested: $BYPASS_REASON\n\nDo you want to proceed with the bypass?"
  }
}
EOF
        exit 0
    else
        echo "${GREEN}✅ No issues found - proceeding with commit${NC}" >&2
        
        # Return JSON for hooks - allow
        output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "No issues found"
  }
}
EOF
        exit 0
    fi
fi

# Handle Reviewed: allows minor issues but NOT major ones
if [[ "$HAS_REVIEWED" == "true" ]]; then
    if [[ $REVIEW_EXIT_CODE -eq 2 ]]; then
        echo "${RED}❌ Code review found MAJOR issues${NC}" >&2
        echo "${RED}The 'Reviewed' acknowledgment only bypasses minor issues.${NC}" >&2
        echo "${RED}For major issues, you must use: Bypass-Review: <reason>${NC}" >&2
        echo "" >&2
        
        # Extract issues for JSON response
        ISSUES_FOUND=$(echo "$REVIEW_OUTPUT" | sed -n '/Issues Found:/,/Review Result:/p' | grep -v "Review Result:" | head -20)
        
        # Return JSON for hooks - deny
        output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "MAJOR issues found that cannot be bypassed with 'Reviewed' acknowledgment:\n\n$ISSUES_FOUND\n\nTo bypass major issues, use: Bypass-Review: <reason why this is safe>"
  }
}
EOF
        exit 0
    elif [[ $REVIEW_EXIT_CODE -eq 1 ]]; then
        echo "${YELLOW}⚠️  Minor issues found but proceeding with review acknowledgment${NC}" >&2
        echo "${GREEN}✅ Commit approved - you've acknowledged the warnings${NC}" >&2
        
        # Return JSON for hooks - allow
        output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "Minor issues acknowledged with Reviewed: HEAD-$HEAD_COMMIT"
  }
}
EOF
        exit 0
    else
        echo "${GREEN}✅ No issues found - proceeding with commit${NC}" >&2
        
        # Return JSON for hooks - allow
        output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "No issues found"
  }
}
EOF
        exit 0
    fi
fi

# Without sentinel, block on ANY issues (exit code 1 or 2)
if [[ $REVIEW_EXIT_CODE -eq 0 ]]; then
    # No issues found
    echo "${GREEN}✅ All checks passed, ready to commit${NC}" >&2
    
    # Return JSON for hooks - allow
    output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "All checks passed"
  }
}
EOF
    exit 0
else
    # Issues found - provide appropriate guidance based on severity
    if [[ $REVIEW_EXIT_CODE -eq 1 ]]; then
        # Minor issues - less intimidating message
        echo "${YELLOW}⚠ Code review found minor issues${NC}" >&2
        echo "General advice: if it's easy to fix, just fix it now. There's no time like the present." >&2
        echo "If it's wrong, just proceed." >&2
        echo "If it's hard and not important enough to fix, track the fix somewhere - with a TODO comment or similar, and acknowledge in the commit message." >&2
        echo "" >&2
        echo "${BOLD}To proceed anyway, add this to your commit message:${NC}" >&2
        echo "   ${YELLOW}Reviewed: HEAD-$HEAD_COMMIT${NC}" >&2
        echo "" >&2
        echo "This acknowledges you've reviewed the warnings and decided to proceed." >&2
        echo "" >&2
        echo "${CYAN}Your staged changes are ready. Fix issues or add the sentinel phrase and retry.${NC}" >&2
        
        # Extract the issues from review output for JSON response
        ISSUES_FOUND=$(echo "$REVIEW_OUTPUT" | sed -n '/Issues Found:/,/^$/p' | grep -v "^$" || echo "Minor issues detected")
        
        # Return JSON for hooks - deny with helpful message including actual issues
        output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Code review found minor issues that should be addressed:\n\n$ISSUES_FOUND\n\nTo bypass with acknowledgment, add to your commit message:\n• Reviewed: HEAD-$HEAD_COMMIT\n\nThis confirms you've reviewed the warnings and decided to proceed."
  }
}
EOF
    else
        # Major issues - stronger message
        echo "${RED}❌ Code review found blocking issues${NC}" >&2
        echo "" >&2
        echo "${BOLD}These issues must be fixed before committing.${NC}" >&2
        echo "The code appears to have serious problems that could break the build or cause runtime errors." >&2
        echo "" >&2
        echo "${CYAN}Your staged changes are ready. Fix the issues and retry.${NC}" >&2
        
        # Extract the issues from review output for JSON response
        ISSUES_FOUND=$(echo "$REVIEW_OUTPUT" | sed -n '/Issues Found:/,/^$/p' | grep -v "^$" || echo "BLOCKING issues detected")
        
        # Return JSON for hooks - deny
        output_json << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Code review found BLOCKING issues that appear to be serious problems:\n\n$ISSUES_FOUND\n\nThese should be fixed before committing.\n\nTo override (use with caution), add to your commit message:\n• Bypass-Review: <specific reason why this is safe despite the issues>"
  }
}
EOF
    fi
    exit 0
fi

# Note: The cleanup function will always restore stashed changes on exit
# This ensures the user's working directory is returned to its original state

