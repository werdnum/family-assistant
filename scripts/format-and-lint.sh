#!/bin/sh

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Timing function
timer_start() {
    START_TIME=$(date +%s)
}

timer_end() {
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - START_TIME))
    echo " (${ELAPSED}s)"
}

# Check for --fast flag
FAST_MODE=0
if [ "$1" = "--fast" ]; then
    FAST_MODE=1
    shift # Remove --fast from arguments
fi

# Default to src and tests directories if no arguments provided
if [ $# -eq 0 ]; then
    TARGETS="src tests"
else
    # Filter arguments to only include Python files and directories
    TARGETS=""
    for arg in "$@"; do
        if [ -d "$arg" ]; then
            # If it's a directory, include it
            TARGETS="$TARGETS $arg"
        elif [ -f "$arg" ] && echo "$arg" | grep -q '\.py$'; then
            # If it's a Python file, include it
            TARGETS="$TARGETS $arg"
        elif [ -f "$arg" ]; then
            # If it's a non-Python file, skip it with a warning
            echo "Warning: Skipping non-Python file: $arg"
        else
            echo "Error: File or directory not found: $arg"
            exit 1
        fi
    done
    
    # If no valid targets after filtering, exit
    if [ -z "$TARGETS" ]; then
        echo "Error: No Python files or directories to lint"
        exit 1
    fi
fi

# Overall timer
OVERALL_START=$(date +%s)

echo "${BLUE}🚀 Running quick checks...${NC}"
echo ""

# Phase 1: Fast sequential checks (fail fast)
# Ruff check
echo -n "${BLUE}  ▸ Running ruff check...${NC}"
timer_start
if ! ${VIRTUAL_ENV:-.venv}/bin/ruff check --fix --preview --ignore=E501 $TARGETS 2>&1; then
    timer_end
    echo ""
    echo "${YELLOW}💡 Showing suggested fixes (including unsafe ones):${NC}"
    ${VIRTUAL_ENV:-.venv}/bin/ruff check --unsafe-fixes --diff --preview --ignore=E501 $TARGETS
    echo ""
    echo "${RED}❌ ruff check failed. Fix the issues above and try again. Use ruff check --fix --unsafe-fixes to apply.${NC}"
    exit 1
fi
echo -n "${GREEN} ✓${NC}"
timer_end

# Ruff format
echo -n "${BLUE}  ▸ Running ruff format...${NC}"
timer_start
if ! ${VIRTUAL_ENV:-.venv}/bin/ruff format --preview $TARGETS 2>&1; then
    timer_end
    echo ""
    echo "${RED}❌ ruff format failed${NC}"
    exit 1
fi
echo -n "${GREEN} ✓${NC}"
timer_end

# mdformat (optional, for markdown files)
if command -v ${VIRTUAL_ENV:-.venv}/bin/mdformat >/dev/null 2>&1; then
    # Find markdown files
    MD_FILES=$(find . -name "*.md" -not -path "./.venv/*" -not -path "./venv/*" -not -path "./.git/*" -not -path "./node_modules/*" 2>/dev/null | head -20)
    if [ -n "$MD_FILES" ]; then
        echo -n "${BLUE}  ▸ Running mdformat...${NC}"
        timer_start
        # Run mdformat but don't fail the script if it fails
        if ${VIRTUAL_ENV:-.venv}/bin/mdformat --wrap 100 $MD_FILES 2>/dev/null; then
            echo -n "${GREEN} ✓${NC}"
        else
            echo -n "${YELLOW} ⚠ (optional)${NC}"
        fi
        timer_end
    fi
fi

# Exit early if in fast mode
if [ $FAST_MODE -eq 1 ]; then
    OVERALL_END=$(date +%s)
    TOTAL_TIME=$((OVERALL_END - OVERALL_START))
    echo ""
    echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo "${BLUE}Total time: ${TOTAL_TIME}s (fast mode)${NC}"
    echo "${GREEN}Quick checks passed! 🚀${NC}"
    exit 0
fi

echo ""
echo "${BLUE}🔍 Running deep analysis (in parallel)...${NC}"
echo ""

# Phase 2: Parallel slow checks
# Start basedpyright
echo "${BLUE}  ▸ Starting basedpyright...${NC}"
timer_start
${VIRTUAL_ENV:-.venv}/bin/basedpyright $TARGETS &
PYRIGHT_PID=$!
PYRIGHT_START=$START_TIME

# Start pylint
echo "${BLUE}  ▸ Starting pylint...${NC}"
timer_start
${VIRTUAL_ENV:-.venv}/bin/pylint -j0 $TARGETS &
PYLINT_PID=$!
PYLINT_START=$START_TIME

# Start frontend linting
echo "${BLUE}  ▸ Starting frontend linting...${NC}"
timer_start
(cd frontend && npm run check) &
FRONTEND_PID=$!
FRONTEND_START=$START_TIME

# Wait for processes and collect results
PYRIGHT_EXIT=0
PYLINT_EXIT=0
FRONTEND_EXIT=0

# Wait for pyright
if wait $PYRIGHT_PID; then
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - PYRIGHT_START))
    echo "${GREEN}  ✓ basedpyright completed successfully (${ELAPSED}s)${NC}"
else
    PYRIGHT_EXIT=$?
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - PYRIGHT_START))
    echo "${RED}  ❌ basedpyright failed (${ELAPSED}s)${NC}"
fi

# Wait for pylint
if wait $PYLINT_PID; then
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - PYLINT_START))
    echo "${GREEN}  ✓ pylint completed successfully (${ELAPSED}s)${NC}"
else
    PYLINT_EXIT=$?
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - PYLINT_START))
    echo "${RED}  ❌ pylint failed (${ELAPSED}s)${NC}"
fi

# Wait for frontend linting
if wait $FRONTEND_PID; then
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - FRONTEND_START))
    echo "${GREEN}  ✓ frontend linting completed successfully (${ELAPSED}s)${NC}"
else
    FRONTEND_EXIT=$?
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - FRONTEND_START))
    echo "${RED}  ❌ frontend linting failed (${ELAPSED}s)${NC}"
fi

# Calculate total time
OVERALL_END=$(date +%s)
TOTAL_TIME=$((OVERALL_END - OVERALL_START))

echo ""
echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "${BLUE}Total time: ${TOTAL_TIME}s${NC}"

# Exit with appropriate code
if [ $PYRIGHT_EXIT -ne 0 ] || [ $PYLINT_EXIT -ne 0 ] || [ $FRONTEND_EXIT -ne 0 ]; then
    echo "${RED}Some checks failed!${NC}"
    exit 1
else
    echo "${GREEN}All checks passed! 🎉${NC}"
    exit 0
fi
