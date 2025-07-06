#!/bin/bash

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Default values
REVIEW_MODE="staged"  # Can be "staged" or "commit"

# Function to display usage
usage() {
    cat << EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

${BOLD}Options:${NC}
    --commit    Review the most recent commit (default: review staged changes)
    --help      Display this help message

${BOLD}Description:${NC}
    This script reviews code changes using an LLM to identify potential issues
    categorized by severity. It exits with different codes based on findings:
    
    Exit 0: No issues or only style/suggestions
    Exit 1: Minor issues (best practices, minor design flaws)
    Exit 2: Major issues (build breaks, runtime errors, security risks)

${BOLD}Examples:${NC}
    $0                  # Review staged changes
    $0 --commit         # Review most recent commit

EOF
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --commit)
            REVIEW_MODE="commit"
            shift
            ;;
        --help|-h)
            usage
            ;;
        *)
            echo "${RED}Error: Unknown option $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Check if git is available
if ! command -v git &> /dev/null; then
    echo "${RED}Error: git command not found${NC}"
    exit 1
fi

# Check if llm is available
if ! command -v llm &> /dev/null; then
    echo "${RED}Error: llm command not found. Please install it with: pip install llm${NC}"
    exit 1
fi

# Check if we're in a git repository
if ! git rev-parse --git-dir &> /dev/null; then
    echo "${RED}Error: Not in a git repository${NC}"
    exit 1
fi

# Get the repository root
REPO_ROOT=$(git rev-parse --show-toplevel)

# Check if CLAUDE.md exists
if [[ ! -f "$REPO_ROOT/CLAUDE.md" ]]; then
    echo "${YELLOW}Warning: CLAUDE.md not found in repository root${NC}"
fi

# Check if REVIEW_GUIDELINES.md exists
if [[ ! -f "$REPO_ROOT/REVIEW_GUIDELINES.md" ]]; then
    echo "${RED}Error: REVIEW_GUIDELINES.md not found in repository root${NC}"
    echo "This file is required for the review process"
    exit 1
fi

echo "${BLUE}🔍 Code Review Tool${NC}"
echo ""

# Determine what to review and get the diff
if [[ "$REVIEW_MODE" == "staged" ]]; then
    echo "${CYAN}Reviewing staged changes...${NC}"
    
    # Check if there are any staged changes
    if ! git diff --cached --quiet; then
        DIFF=$(git diff --cached)
        DIFF_STAT=$(git diff --cached --stat)
    else
        echo "${YELLOW}No staged changes to review${NC}"
        exit 0
    fi
else
    echo "${CYAN}Reviewing most recent commit...${NC}"
    DIFF=$(git show HEAD)
    DIFF_STAT=$(git show HEAD --stat)
fi

# Create a temporary file for the LLM response
TEMP_RESPONSE=$(mktemp /tmp/review-response.XXXXXX.json)
TEMP_STDERR=$(mktemp /tmp/review-stderr.XXXXXX.log)
trap "rm -f $TEMP_RESPONSE $TEMP_STDERR" EXIT

# Define the JSON schema for structured output
SCHEMA='{
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One paragraph describing the changes"
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["BREAKS_BUILD", "RUNTIME_ERROR", "SECURITY_RISK", "LOGIC_ERROR", "DESIGN_FLAW_MAJOR", "DESIGN_FLAW_MINOR", "BEST_PRACTICE", "STYLE", "SUGGESTION"],
                        "description": "Issue severity level"
                    },
                    "file": {
                        "type": "string",
                        "description": "File path where issue was found"
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number (optional)"
                    },
                    "description": {
                        "type": "string",
                        "description": "Clear description of the issue"
                    },
                    "suggestion": {
                        "type": "string",
                        "description": "How to fix the issue"
                    }
                },
                "required": ["severity", "file", "description", "suggestion"]
            },
            "description": "List of issues found"
        },
        "positive_aspects": {
            "type": "array",
            "items": {
                "type": "string"
            },
            "description": "Good practices observed in the changes"
        },
        "stats": {
            "type": "object",
            "properties": {
                "files_changed": {"type": "integer"},
                "lines_added": {"type": "integer"},
                "lines_removed": {"type": "integer"}
            },
            "description": "Change statistics"
        }
    },
    "required": ["summary", "issues", "positive_aspects", "stats"]
}'

# System prompt for the review
SYSTEM_PROMPT="You are a code reviewer analyzing git diffs. Review the changes according to the guidelines provided, categorizing any issues by their severity level. Be constructive and specific. Focus on actual problems rather than style preferences unless they violate project standards."

echo "${CYAN}Analyzing changes...${NC}"
echo ""

# Show diff stats
echo "${BOLD}Change Statistics:${NC}"
echo "$DIFF_STAT"
echo ""

# Call LLM with the diff
echo "${CYAN}Running review analysis...${NC}"

# Create the review prompt
REVIEW_PROMPT="Review the following git diff and identify any issues according to the severity levels defined in the guidelines. Be thorough but focus on actual problems.

DIFF:
$DIFF"

# Execute LLM call (redirect stderr to a separate file for debugging)
if llm -f "$REPO_ROOT/CLAUDE.md" -f "$REPO_ROOT/REVIEW_GUIDELINES.md" \
    --schema "$SCHEMA" \
    -s "$SYSTEM_PROMPT" \
    "$REVIEW_PROMPT" > "$TEMP_RESPONSE" 2>"$TEMP_STDERR"; then
    
    # Parse the JSON response
    if ! jq empty "$TEMP_RESPONSE" 2>/dev/null; then
        echo "${RED}Error: Invalid JSON response from LLM${NC}"
        cat "$TEMP_RESPONSE"
        exit 1
    fi
else
    echo "${RED}Error: LLM call failed${NC}"
    if [[ -s "$TEMP_STDERR" ]]; then
        echo "${YELLOW}Error details:${NC}"
        cat "$TEMP_STDERR"
    fi
    exit 1
fi

# Extract data from JSON response
SUMMARY=$(jq -r '.summary' "$TEMP_RESPONSE")
ISSUE_COUNT=$(jq '.issues | length' "$TEMP_RESPONSE")

echo ""
echo "${BOLD}Summary:${NC}"
echo "$SUMMARY"
echo ""

# Function to get severity color
get_severity_color() {
    case $1 in
        "BREAKS_BUILD"|"RUNTIME_ERROR"|"SECURITY_RISK"|"LOGIC_ERROR"|"DESIGN_FLAW_MAJOR")
            echo "$RED"
            ;;
        "DESIGN_FLAW_MINOR"|"BEST_PRACTICE")
            echo "$YELLOW"
            ;;
        "STYLE"|"SUGGESTION")
            echo "$CYAN"
            ;;
        *)
            echo "$NC"
            ;;
    esac
}

# Display issues grouped by severity
if [[ $ISSUE_COUNT -gt 0 ]]; then
    echo "${BOLD}Issues Found:${NC}"
    echo ""
    
    # Group issues by severity
    for severity in "BREAKS_BUILD" "RUNTIME_ERROR" "SECURITY_RISK" "LOGIC_ERROR" "DESIGN_FLAW_MAJOR" "DESIGN_FLAW_MINOR" "BEST_PRACTICE" "STYLE" "SUGGESTION"; do
        ISSUES_FOR_SEVERITY=$(jq -r --arg sev "$severity" '.issues[] | select(.severity == $sev) | @json' "$TEMP_RESPONSE")
        
        if [[ -n "$ISSUES_FOR_SEVERITY" ]]; then
            COLOR=$(get_severity_color "$severity")
            echo "${COLOR}${BOLD}$severity:${NC}"
            
            echo "$ISSUES_FOR_SEVERITY" | while IFS= read -r issue_json; do
                FILE=$(echo "$issue_json" | jq -r '.file')
                LINE=$(echo "$issue_json" | jq -r '.line // ""')
                DESC=$(echo "$issue_json" | jq -r '.description')
                SUGG=$(echo "$issue_json" | jq -r '.suggestion')
                
                if [[ -n "$LINE" ]]; then
                    echo "  ${BOLD}$FILE:$LINE${NC}"
                else
                    echo "  ${BOLD}$FILE${NC}"
                fi
                echo "    ${COLOR}Issue:${NC} $DESC"
                echo "    ${GREEN}Fix:${NC} $SUGG"
                echo ""
            done
        fi
    done
else
    echo "${GREEN}✓ No issues found!${NC}"
    echo ""
fi

# Display positive aspects
POSITIVE_COUNT=$(jq '.positive_aspects | length' "$TEMP_RESPONSE")
if [[ $POSITIVE_COUNT -gt 0 ]]; then
    echo "${GREEN}${BOLD}Positive Aspects:${NC}"
    jq -r '.positive_aspects[]' "$TEMP_RESPONSE" | while IFS= read -r aspect; do
        echo "  ${GREEN}✓${NC} $aspect"
    done
    echo ""
fi

# Determine exit code based on highest severity
HIGHEST_SEVERITY=""
EXIT_CODE=0

# Check for blocking issues (exit code 2)
for severity in "BREAKS_BUILD" "RUNTIME_ERROR" "SECURITY_RISK" "LOGIC_ERROR" "DESIGN_FLAW_MAJOR"; do
    if jq -e --arg sev "$severity" '.issues[] | select(.severity == $sev)' "$TEMP_RESPONSE" >/dev/null 2>&1; then
        HIGHEST_SEVERITY=$severity
        EXIT_CODE=2
        break
    fi
done

# Check for warning issues (exit code 1) if no blocking issues found
if [[ $EXIT_CODE -eq 0 ]]; then
    for severity in "DESIGN_FLAW_MINOR" "BEST_PRACTICE"; do
        if jq -e --arg sev "$severity" '.issues[] | select(.severity == $sev)' "$TEMP_RESPONSE" >/dev/null 2>&1; then
            HIGHEST_SEVERITY=$severity
            EXIT_CODE=1
            break
        fi
    done
fi

# Final verdict
echo "${BOLD}Review Result:${NC}"
if [[ $EXIT_CODE -eq 0 ]]; then
    echo "${GREEN}✓ All checks passed!${NC}"
elif [[ $EXIT_CODE -eq 1 ]]; then
    echo "${YELLOW}⚠ Minor issues found (highest: $HIGHEST_SEVERITY)${NC}"
    echo "Consider addressing these before merging."
else
    echo "${RED}✗ Blocking issues found (highest: $HIGHEST_SEVERITY)${NC}"
    echo "These must be fixed before the code can be merged."
fi

echo ""
echo "Exit code: $EXIT_CODE"

exit $EXIT_CODE
