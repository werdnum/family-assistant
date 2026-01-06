#!/bin/bash

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Summary file for easy result checking (useful when output is truncated)
SUMMARY_FILE=".poe-test-summary.txt"
# Track all background processes for cleanup
BACKGROUND_PIDS=()
CLEANUP_RUNNING=0

# Cleanup function - kills all background processes
cleanup() {
    # Prevent recursive cleanup calls
    if [ "$CLEANUP_RUNNING" -eq 1 ]; then
        return
    fi
    CLEANUP_RUNNING=1

    local exit_code=$?

    # If we have background processes, clean them up
    if [ ${#BACKGROUND_PIDS[@]} -gt 0 ]; then
        echo ""
        echo "${YELLOW}⚠️  Interrupted - cleaning up background processes...${NC}" >&2

        # Send SIGTERM to all background processes
        for pid in "${BACKGROUND_PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
            fi
        done

        # Wait up to 3 seconds for graceful shutdown
        local wait_count=0
        while [ "$wait_count" -lt 30 ]; do
            local any_alive=0
            for pid in "${BACKGROUND_PIDS[@]}"; do
                if kill -0 "$pid" 2>/dev/null; then
                    # Check if process is a zombie
                    local state
                    state=$(ps -o state= -p "$pid" 2>/dev/null || true)
                    if [ "$state" != "Z" ] && [ -n "$state" ]; then
                        any_alive=1
                        break
                    fi
                fi
            done

            if [ "$any_alive" -eq 0 ]; then
                break
            fi

            sleep 0.1
            wait_count=$((wait_count + 1))
        done

        # Force kill any remaining processes
        for pid in "${BACKGROUND_PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                echo "${YELLOW}  Force killing process $pid${NC}" >&2
                kill -9 "$pid" 2>/dev/null || true
            fi
        done

        echo "${GREEN}✓ Cleanup complete${NC}" >&2
    fi

    # Exit with appropriate code (130 for SIGINT)
    if [ "$exit_code" -eq 0 ] && [ -n "${INTERRUPTED:-}" ]; then
        exit 130
    else
        exit "$exit_code"
    fi
}

# Handle interruption signals
handle_signal() {
    INTERRUPTED=1
    echo ""
    echo "${YELLOW}⚠️  Received interrupt signal${NC}" >&2
    cleanup
}

# Set up signal traps
trap handle_signal SIGINT SIGTERM
trap cleanup EXIT

# Usage function
usage() {
    echo "Usage: $0 [options] [pytest-args]"
    echo ""
    echo "Options:"
    echo "  --skip-lint      Skip linting and only run tests"
    echo "  -n NUM           Set pytest parallelism (e.g., -n2, -n4, -n auto)"
    echo "  --help           Show this help message"
    echo ""
    echo "Environment Variables:"
    echo "  PYTEST_PARALLELISM    Set default pytest parallelism (e.g., 4, auto, 1)"
    echo "                        Overridden by -n command line option"
    echo ""
    echo "Examples:"
    echo "  $0                    # Run linting and tests with default parallelism"
    echo "  $0 -n2                # Run with 2 parallel workers"
    echo "  $0 -n1                # Run tests sequentially"
    echo "  $0 --skip-lint -n2    # Skip linting, run tests with 2 workers"
    echo "  PYTEST_PARALLELISM=4 $0    # Run with 4 workers via env var"
    echo ""
    echo "All other arguments are passed directly to pytest."
    exit 0
}

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
PYTEST_ARGS=()
PARALLELISM=""

while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            ;;
        --skip-lint)
            SKIP_LINT=1
            shift
            ;;
        -n|--numprocesses)
            # Capture parallelism setting
            if [ -z "$2" ] || [ "${2#-}" != "$2" ]; then
                echo "${RED}Error: Argument for $1 is missing or is another option${NC}" >&2
                exit 1
            fi
            PARALLELISM="$1 $2"
            shift 2
            ;;
        -n*)
            # Handle -n2, -n4, etc.
            PARALLELISM="$1"
            shift
            ;;
        *)
            # All other arguments are passed to pytest
            PYTEST_ARGS+=("$1")
            shift
            ;;
    esac
done

# Check for PYTEST_PARALLELISM environment variable if no -n was provided
if [ -z "$PARALLELISM" ] && [ -n "$PYTEST_PARALLELISM" ]; then
    PARALLELISM="-n$PYTEST_PARALLELISM"
fi

# Auto-select SQLite backend when PostgreSQL isn't available
if ! echo " ${PYTEST_ARGS[*]} " | grep -Eq ' --db(=| )| --postgres '; then
    if command -v docker >/dev/null 2>&1 || \
       command -v podman >/dev/null 2>&1 || \
       [ -n "$TEST_DATABASE_URL" ]; then
        :
    else
        echo "${YELLOW}PostgreSQL not detected - running SQLite-only tests${NC}"
        PYTEST_ARGS=("--db" "sqlite" "${PYTEST_ARGS[@]}")
    fi
fi

# Default pytest arguments if none provided
if [ ${#PYTEST_ARGS[@]} -eq 0 ]; then
    PYTEST_ARGS=("tests" "--ignore=scratch")
fi

# Ensure dependencies are installed
echo "${BLUE}Ensuring all dependencies are installed...${NC}"
if ! uv sync --extra dev --extra pgserver; then
    echo "${RED}❌ Dependency installation failed.${NC}" >&2
    exit 1
fi
echo "${GREEN}✓ Dependencies are up to date${NC}"

# Acquire exclusive lock to prevent concurrent test runs
LOCK_FILE="$HOME/.poe-test.lock"
exec 200>"$LOCK_FILE"

echo "${BLUE}Acquiring test lock...${NC}"
if ! flock --exclusive --timeout 300 200; then
    echo "${RED}❌ Error: Could not acquire lock after waiting 5 minutes.${NC}" >&2
    echo "${YELLOW}Tests are currently running in another process.${NC}" >&2
    echo "${YELLOW}  Lock file: $LOCK_FILE${NC}" >&2

    # Try to show which process holds the lock
    if command -v lsof >/dev/null 2>&1; then
        LOCK_HOLDER=$(lsof "$LOCK_FILE" 2>/dev/null | tail -n +2 | awk '{print $2}')
        if [ -n "$LOCK_HOLDER" ]; then
            echo "${YELLOW}  Process holding lock: PID $LOCK_HOLDER${NC}" >&2
            ps -p "$LOCK_HOLDER" -o pid,cmd 2>/dev/null | grep -v "PID CMD" | sed "s/^/${YELLOW}    /" | sed "s/$/${NC}/" >&2
        fi
    fi

    echo "" >&2
    echo "${YELLOW}Please wait for the other test run to complete.${NC}" >&2
    echo "${YELLOW}Do NOT remove the lock file - it will be released automatically.${NC}" >&2
    exit 1
fi
echo "${GREEN}✓ Lock acquired${NC}"

# Overall timer
OVERALL_START=$(date +%s)

# Show summary file location at start (helpful when output is truncated)
echo ""
echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "${BLUE}  Results will be written to: ${SUMMARY_FILE}${NC}"
echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

if [ $SKIP_LINT -eq 0 ]; then
    echo "${BLUE}🚀 Running quick checks...${NC}"
    echo ""

    # Phase 1: Fast sequential checks (fail fast)
    # Ruff check
    echo -n "${BLUE}  ▸ Running ruff check...${NC}"
    timer_start
    if ! "${VIRTUAL_ENV:-.venv}"/bin/ruff check --fix --preview --ignore=E501 src tests 2>&1; then
        timer_end
        echo ""
        echo "${YELLOW}💡 Showing suggested fixes (including unsafe ones):${NC}"
        "${VIRTUAL_ENV:-.venv}"/bin/ruff check --unsafe-fixes --diff --preview --ignore=E501 src tests
        echo ""
        echo "${RED}❌ ruff check failed. Fix the issues above and try again. Use ruff check --fix --unsafe-fixes to apply.${NC}"
        exit 1
    fi
    echo -n "${GREEN} ✓${NC}"
    timer_end

    # Ruff format
    echo -n "${BLUE}  ▸ Running ruff format...${NC}"
    timer_start
    if ! "${VIRTUAL_ENV:-.venv}"/bin/ruff format --preview src tests 2>&1; then
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

# Force a rebuild of the frontend
echo -n "${BLUE}  ▸ Building frontend...${NC}"
FRONTEND_BUILD_LOG=$(mktemp)
timer_start
if (cd frontend && npm run build > "$FRONTEND_BUILD_LOG" 2>&1); then
    echo -n "${GREEN} ✓${NC}"
    timer_end
    rm -f "$FRONTEND_BUILD_LOG"
else
    timer_end
    echo ""
    echo "${RED}❌ Frontend build failed:${NC}"
    cat "$FRONTEND_BUILD_LOG"
    rm -f "$FRONTEND_BUILD_LOG"
    exit 1
fi

# Phase 2: Parallel execution of tests and analysis

# Start pytest (always runs)
echo "${BLUE}  ▸ Starting pytest...${NC}"
TEST_START=$(date +%s)
if [ "${USE_MEMORY_LIMIT:-0}" = "1" ]; then
    scripts/run_with_memory_limit.sh "${VIRTUAL_ENV:-.venv}"/bin/pytest --json-report --json-report-file=.report.json --disable-warnings --tb=short -q --ignore=scratch "$PARALLELISM" "${PYTEST_ARGS[@]}" &
else
    "${VIRTUAL_ENV:-.venv}"/bin/pytest --json-report --json-report-file=.report.json --disable-warnings --tb=short -q --ignore=scratch "$PARALLELISM" "${PYTEST_ARGS[@]}" &
fi
TEST_PID=$!
BACKGROUND_PIDS+=("$TEST_PID")

# Start frontend unit tests (always runs)
echo "${BLUE}  ▸ Starting frontend unit tests...${NC}"
FRONTEND_TEST_START=$(date +%s)
FRONTEND_TEST_LOG=$(mktemp)
(cd frontend && npm run test -- --run > "$FRONTEND_TEST_LOG" 2>&1) &
FRONTEND_TEST_PID=$!
BACKGROUND_PIDS+=("$FRONTEND_TEST_PID")

# Start linting and type checking only if not skipped
if [ $SKIP_LINT -eq 0 ]; then
    # Start basedpyright
    echo "${BLUE}  ▸ Starting basedpyright...${NC}"
    PYRIGHT_START=$(date +%s)
    "${VIRTUAL_ENV:-.venv}"/bin/basedpyright src tests &
    PYRIGHT_PID=$!
    BACKGROUND_PIDS+=("$PYRIGHT_PID")

    # Start pylint
    echo "${BLUE}  ▸ Starting pylint...${NC}"
    PYLINT_START=$(date +%s)
    "${VIRTUAL_ENV:-.venv}"/bin/pylint -j0 src tests &
    PYLINT_PID=$!
    BACKGROUND_PIDS+=("$PYLINT_PID")

    # Start frontend linting
    echo "${BLUE}  ▸ Starting frontend linting...${NC}"
    FRONTEND_START=$(date +%s)
    (cd frontend && exec npm run check) &
    FRONTEND_PID=$!
    BACKGROUND_PIDS+=("$FRONTEND_PID")

    # Start frontend TypeScript type checking
    echo "${BLUE}  ▸ Starting frontend TypeScript type checking...${NC}"
    FRONTEND_TS_START=$(date +%s)
    (cd frontend && exec npm run typecheck) &
    FRONTEND_TS_PID=$!
    BACKGROUND_PIDS+=("$FRONTEND_TS_PID")
fi

# Wait for processes and collect results
PYRIGHT_EXIT=0
TEST_EXIT=0
PYLINT_EXIT=0
FRONTEND_EXIT=0
FRONTEND_TS_EXIT=0
FRONTEND_TEST_EXIT=0

# Wait for pytest (always running)
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

# Wait for frontend unit tests (always running)
if wait $FRONTEND_TEST_PID; then
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - FRONTEND_TEST_START))
    echo "${GREEN}  ✓ Frontend unit tests complete (${ELAPSED}s)${NC}"
    rm -f "$FRONTEND_TEST_LOG"
else
    FRONTEND_TEST_EXIT=$?
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - FRONTEND_TEST_START))
    echo "${RED}  ❌ Frontend unit tests failed (${ELAPSED}s)${NC}"
    echo ""
    echo "${RED}Frontend test output:${NC}"
    cat "$FRONTEND_TEST_LOG"
    rm -f "$FRONTEND_TEST_LOG"
fi

# Wait for linting/type checking only if they were started
if [ $SKIP_LINT -eq 0 ]; then
    # Wait for pyright
    if wait "$PYRIGHT_PID"; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - PYRIGHT_START))
        echo "${GREEN}  ✓ Type checking complete (${ELAPSED}s)${NC}"
    else
        PYRIGHT_EXIT=$?
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - PYRIGHT_START))
        echo "${RED}  ❌ Type checking failed (${ELAPSED}s)${NC}"
    fi

    # Wait for pylint
    if wait "$PYLINT_PID"; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - PYLINT_START))
        echo "${GREEN}  ✓ Linting complete (${ELAPSED}s)${NC}"
    else
        PYLINT_EXIT=$?
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - PYLINT_START))
        echo "${RED}  ❌ Linting failed (${ELAPSED}s)${NC}"
    fi

    # Wait for frontend linting
    if wait "$FRONTEND_PID"; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - FRONTEND_START))
        echo "${GREEN}  ✓ Frontend linting complete (${ELAPSED}s)${NC}"
    else
        FRONTEND_EXIT=$?
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - FRONTEND_START))
        echo "${RED}  ❌ Frontend linting failed (${ELAPSED}s)${NC}"
    fi

    # Wait for frontend TypeScript type checking
    if wait "$FRONTEND_TS_PID"; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - FRONTEND_TS_START))
        echo "${GREEN}  ✓ Frontend TypeScript type checking complete (${ELAPSED}s)${NC}"
    else
        FRONTEND_TS_EXIT=$?
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - FRONTEND_TS_START))
        echo "${RED}  ❌ Frontend TypeScript type checking failed (${ELAPSED}s)${NC}"
    fi
fi

# Calculate total time
OVERALL_END=$(date +%s)
TOTAL_TIME=$((OVERALL_END - OVERALL_START))

# Build status summary
FAILED_CHECKS=()
PASSED_CHECKS=()

if [ $TEST_EXIT -ne 0 ]; then
    FAILED_CHECKS+=("pytest")
else
    PASSED_CHECKS+=("pytest")
fi

if [ $FRONTEND_TEST_EXIT -ne 0 ]; then
    FAILED_CHECKS+=("frontend-tests")
else
    PASSED_CHECKS+=("frontend-tests")
fi

if [ $SKIP_LINT -eq 0 ]; then
    if [ $PYRIGHT_EXIT -ne 0 ]; then
        FAILED_CHECKS+=("basedpyright")
    else
        PASSED_CHECKS+=("basedpyright")
    fi

    if [ $PYLINT_EXIT -ne 0 ]; then
        FAILED_CHECKS+=("pylint")
    else
        PASSED_CHECKS+=("pylint")
    fi

    if [ $FRONTEND_EXIT -ne 0 ]; then
        FAILED_CHECKS+=("frontend-lint")
    else
        PASSED_CHECKS+=("frontend-lint")
    fi

    if [ $FRONTEND_TS_EXIT -ne 0 ]; then
        FAILED_CHECKS+=("frontend-typecheck")
    else
        PASSED_CHECKS+=("frontend-typecheck")
    fi
fi

echo ""
echo ""

# Write summary to file for easy checking when output is truncated
write_summary() {
    {
        echo "=== poe test summary ==="
        echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Duration: ${TOTAL_TIME}s"
        echo ""
        if [ ${#FAILED_CHECKS[@]} -gt 0 ]; then
            echo "RESULT: FAILED"
            echo "Failed (${#FAILED_CHECKS[@]}): ${FAILED_CHECKS[*]}"
        else
            echo "RESULT: PASSED"
        fi
        if [ ${#PASSED_CHECKS[@]} -gt 0 ]; then
            echo "Passed (${#PASSED_CHECKS[@]}): ${PASSED_CHECKS[*]}"
        fi
    } > "$SUMMARY_FILE"
}

# Exit with appropriate code
if [ ${#FAILED_CHECKS[@]} -gt 0 ]; then
    write_summary
    echo "${RED}╔════════════════════════════════════════════════════════════╗${NC}"
    echo "${RED}║                      ❌ FAILED                             ║${NC}"
    echo "${RED}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "${RED}Failed (${#FAILED_CHECKS[@]}): ${FAILED_CHECKS[*]}${NC}"
    if [ ${#PASSED_CHECKS[@]} -gt 0 ]; then
        echo "${GREEN}Passed (${#PASSED_CHECKS[@]}): ${PASSED_CHECKS[*]}${NC}"
    fi
    echo "${BLUE}Total time: ${TOTAL_TIME}s${NC}"
    if [ -f .report.json ]; then
        echo ""
        echo "${YELLOW}📊 Test report: .report.json${NC}"
        echo "${YELLOW}   Failed tests: jq '.tests | map(select(.outcome == \"failed\"))' .report.json${NC}"
    fi
    echo ""
    echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo "${YELLOW}Summary: cat $SUMMARY_FILE${NC}"
    echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
else
    write_summary
    echo "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
    echo "${GREEN}║                    ✅ ALL PASSED                           ║${NC}"
    echo "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "${GREEN}Passed (${#PASSED_CHECKS[@]}): ${PASSED_CHECKS[*]}${NC}"
    echo "${BLUE}Total time: ${TOTAL_TIME}s${NC}"
    echo ""
    echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo "${GREEN}Summary: cat $SUMMARY_FILE${NC}"
    echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 0
fi

