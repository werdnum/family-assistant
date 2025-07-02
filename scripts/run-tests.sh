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

# Parse command line arguments
SKIP_LINT=0
PYTEST_ARGS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-lint)
            SKIP_LINT=1
            shift
            ;;
        *)
            # All other arguments are passed to pytest
            PYTEST_ARGS="$PYTEST_ARGS $1"
            shift
            ;;
    esac
done

# Default pytest arguments if none provided
if [ -z "$PYTEST_ARGS" ]; then
    PYTEST_ARGS="tests"
fi

# Overall timer
OVERALL_START=$(date +%s)

if [ $SKIP_LINT -eq 0 ]; then
    echo "${BLUE}🚀 Running quick checks...${NC}"
    echo ""

    # Phase 1: Fast sequential checks (fail fast)
    # Ruff check
    echo -n "${BLUE}  ▸ Running ruff check...${NC}"
    timer_start
    if ! ${VIRTUAL_ENV:-.venv}/bin/ruff check --fix --preview --ignore=E501 src tests 2>&1; then
        timer_end
        echo ""
        echo "${YELLOW}💡 Showing suggested fixes (including unsafe ones):${NC}"
        ${VIRTUAL_ENV:-.venv}/bin/ruff check --unsafe-fixes --diff --preview --ignore=E501 src tests
        echo ""
        echo "${RED}❌ ruff check failed. Fix the issues above and try again. Use ruff check --fix --unsafe-fixes to apply.${NC}"
        exit 1
    fi
    echo -n "${GREEN} ✓${NC}"
    timer_end

    # Ruff format
    echo -n "${BLUE}  ▸ Running ruff format...${NC}"
    timer_start
    if ! ${VIRTUAL_ENV:-.venv}/bin/ruff format --preview src tests 2>&1; then
        timer_end
        echo ""
        echo "${RED}❌ ruff format failed${NC}"
        exit 1
    fi
    echo -n "${GREEN} ✓${NC}"
    timer_end

    echo ""
    echo "${BLUE}🔍 Starting deep analysis and tests in parallel...${NC}"
    echo ""
else
    echo "${BLUE}🔍 Running tests (linting skipped)...${NC}"
    echo ""
fi

# Phase 2: Parallel execution of tests and analysis
if [ $SKIP_LINT -eq 0 ]; then
    # Start basedpyright
    echo "${BLUE}  ▸ Starting basedpyright...${NC}"
    timer_start
    ${VIRTUAL_ENV:-.venv}/bin/basedpyright src tests &
    PYRIGHT_PID=$!
    PYRIGHT_START=$START_TIME

    # Give pyright a moment to start
    sleep 2

    # Start pytest
    echo "${BLUE}  ▸ Starting pytest...${NC}"
    timer_start
    pytest --json-report --json-report-file=.report.json --disable-warnings -q $PYTEST_ARGS &
    TEST_PID=$!
    TEST_START=$START_TIME

    # Start pylint
    echo "${BLUE}  ▸ Starting pylint...${NC}"
    timer_start
    ${VIRTUAL_ENV:-.venv}/bin/pylint -j0 src tests &
    PYLINT_PID=$!
    PYLINT_START=$START_TIME
else
    # Just run pytest when linting is skipped
    echo "${BLUE}  ▸ Starting pytest...${NC}"
    timer_start
    pytest --json-report --json-report-file=.report.json --disable-warnings -q $PYTEST_ARGS &
    TEST_PID=$!
    TEST_START=$START_TIME
fi

# Wait for processes and collect results
PYRIGHT_EXIT=0
TEST_EXIT=0
PYLINT_EXIT=0

if [ $SKIP_LINT -eq 0 ]; then
    # Wait for pyright
    if wait $PYRIGHT_PID; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - PYRIGHT_START))
        echo "${GREEN}  ✓ Type checking complete (${ELAPSED}s)${NC}"
    else
        PYRIGHT_EXIT=$?
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - PYRIGHT_START))
        echo "${RED}  ❌ Type checking failed (${ELAPSED}s)${NC}"
    fi

    # Wait for tests
    if wait $TEST_PID; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - TEST_START))
        echo "${GREEN}  ✓ Tests complete (${ELAPSED}s)${NC}"
    else
        TEST_EXIT=$?
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - TEST_START))
        echo "${RED}  ❌ Tests failed (${ELAPSED}s)${NC}"
    fi

    # Wait for pylint
    if wait $PYLINT_PID; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - PYLINT_START))
        echo "${GREEN}  ✓ Linting complete (${ELAPSED}s)${NC}"
    else
        PYLINT_EXIT=$?
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - PYLINT_START))
        echo "${RED}  ❌ Linting failed (${ELAPSED}s)${NC}"
    fi
else
    # Just wait for tests when linting is skipped
    if wait $TEST_PID; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - TEST_START))
        echo "${GREEN}  ✓ Tests complete (${ELAPSED}s)${NC}"
    else
        TEST_EXIT=$?
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - TEST_START))
        echo "${RED}  ❌ Tests failed (${ELAPSED}s)${NC}"
    fi
fi

# Calculate total time
OVERALL_END=$(date +%s)
TOTAL_TIME=$((OVERALL_END - OVERALL_START))

echo ""
echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "${BLUE}Total time: ${TOTAL_TIME}s${NC}"

# Exit with appropriate code
if [ $PYRIGHT_EXIT -ne 0 ] || [ $TEST_EXIT -ne 0 ] || [ $PYLINT_EXIT -ne 0 ]; then
    echo "${RED}Some checks failed!${NC}"
    if [ -f .report.json ]; then
        echo ""
        echo "${YELLOW}📊 Test results saved to .report.json${NC}"
        echo "${YELLOW}   Use jq to query results:${NC}"
        echo "${YELLOW}   - Failed tests: jq '.tests | map(select(.outcome == \"failed\"))' .report.json${NC}"
        echo "${YELLOW}   - Test summary: jq '.summary' .report.json${NC}"
        echo "${YELLOW}   - Slow tests: jq '.tests | sort_by(.duration) | reverse | .[0:5]' .report.json${NC}"
    fi
    exit 1
else
    echo "${GREEN}All checks passed! 🎉${NC}"
    if [ -f .report.json ]; then
        echo ""
        echo "${GREEN}📊 Test results saved to .report.json${NC}"
        echo "${GREEN}   Use jq to query results:${NC}"
        echo "${GREEN}   - Test summary: jq '.summary' .report.json${NC}"
        echo "${GREEN}   - Slow tests: jq '.tests | sort_by(.duration) | reverse | .[0:5]' .report.json${NC}"
        echo "${GREEN}   - Test count by file: jq '.tests | group_by(.nodeid | split(\":\")[0]) | map({file: .[0].nodeid | split(\":\")[0], count: length})' .report.json${NC}"
    fi
    exit 0
fi