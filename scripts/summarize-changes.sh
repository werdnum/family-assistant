#!/bin/bash

# Configuration
TARGET_BRANCH="origin/main"
MODEL="gemini-3-pro-preview" 
EXCLUDE_SPEC=":!tests/cassettes/*"

# LLM System Prompt
PROMPT="Summarize these changes in detail, one '=== filename' level section per file. 
Use bullet points for each code level change in that file. 
Focus on the logic and functional changes.
Output in clean Markdown."

# --- Logging Helper (Outputs to STDERR only) ---
# This keeps your stdout clean for redirection
log() {
    local COLOR_BLUE="\033[0;34m"
    local COLOR_GREEN="\033[0;32m"
    local COLOR_RESET="\033[0m"
    echo -e "${COLOR_BLUE}[PROGRESS]${COLOR_RESET} $1" >&2
}

# Check if inside a git repo
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "Error: Not inside a git repository." >&2
    exit 1
fi

# Fetch latest origin
log "Fetching $TARGET_BRANCH..."
git fetch origin main > /dev/null 2>&1

# Calculate merge base
MERGE_BASE=$(git merge-base $TARGET_BRANCH HEAD)

if [ -z "$MERGE_BASE" ]; then
    echo "Error: Could not find merge base with $TARGET_BRANCH" >&2
    exit 1
fi

# --- Processing Function ---
process_section() {
    local title="$1"
    local stat_cmd="$2"
    local diff_cmd="$3"
    local extra_content="$4" # For untracked files

    # PRINT HEADER TO STDOUT (Markdown)
    echo "## $title"
    echo ""

    # PRINT STATS TO STDOUT (Inside code block)
    echo '```txt'
    eval "$stat_cmd"
    echo '```'
    echo ""

    # GENERATE DIFF
    local diff_output
    diff_output=$(eval "$diff_cmd")
    
    # Add untracked/extra content if it exists
    if [ ! -z "$extra_content" ]; then
        diff_output="${diff_output}\n${extra_content}"
    fi

    # RUN LLM OR SKIP
    if [ -z "$diff_output" ] || [ "$diff_output" == " " ]; then
        echo "_No substantial code changes (only excluded files or formatting)._"
    else
        log "Generating summary for: $title"
        echo "$diff_output" | llm -m "$MODEL" "$PROMPT"
    fi
    
    echo ""
    echo "---"
    echo ""
}

# ==========================================
# 1. Process Commits
# ==========================================
log "Analyzing commits since $MERGE_BASE..."

# Get commits oldest -> newest
COMMITS=$(git rev-list --reverse $MERGE_BASE..HEAD)

for commit in $COMMITS; do
    SUBJECT=$(git log -1 --format="%s" $commit)
    HASH=$(git log -1 --format="%h" $commit)
    
    process_section \
        "Commit $HASH: $SUBJECT" \
        "git show --stat $commit" \
        "git show $commit -- . '$EXCLUDE_SPEC'"
done

# ==========================================
# 2. Process Staged Changes
# ==========================================
if ! git diff --cached --quiet; then
    process_section \
        "Staged Changes" \
        "git diff --cached --stat" \
        "git diff --cached -- . '$EXCLUDE_SPEC'"
fi

# ==========================================
# 3. Process Unstaged + Untracked Changes
# ==========================================
# Handle untracked files manually
UNTRACKED_FILES=$(git ls-files --others --exclude-standard)
UNTRACKED_CONTENT=""

if [ ! -z "$UNTRACKED_FILES" ]; then
    for f in $UNTRACKED_FILES; do
        # Skip excluded pathspecs
        if [[ "$f" == tests/cassettes/* ]]; then continue; fi
        
        # Only read text files
        if grep -qI "." "$f"; then
            CONTENT=$(cat "$f")
            UNTRACKED_CONTENT="${UNTRACKED_CONTENT}\n\n=== NEW UNTRACKED FILE: $f ===\n${CONTENT}"
        fi
    done
fi

# Run if we have unstaged diffs OR untracked content
if ! git diff --quiet || [ ! -z "$UNTRACKED_CONTENT" ]; then
    # Create a stat command that includes untracked files list
    STAT_CMD="git diff --stat; if [ ! -z '$UNTRACKED_FILES' ]; then echo ''; echo 'Untracked files:'; echo '$UNTRACKED_FILES'; fi"

    process_section \
        "Unstaged & Untracked Changes" \
        "$STAT_CMD" \
        "git diff -- . '$EXCLUDE_SPEC'" \
        "$UNTRACKED_CONTENT"
fi

log "Done."
