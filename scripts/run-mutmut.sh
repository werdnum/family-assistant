#!/usr/bin/env bash
set -euo pipefail

# Mutation testing wrapper for mutmut v3.
#
# Usage:
#   scripts/run-mutmut.sh --build-map                     # Step 1: generate coverage + test mapping
#   scripts/run-mutmut.sh [mutant_pattern]                # Step 2: run mutmut (optionally with pattern)
#   scripts/run-mutmut.sh --narrow family_assistant.tools  # Step 2: run with narrowed test selection
#
# Options:
#   --build-map        Generate coverage data and test mapping (run first)
#   --narrow MODULE    Use coverage map to narrow test selection for MODULE
#   --all-tests        Don't narrow test selection
#   --debug            Enable mutmut debug output
#
# The --build-map step runs pytest with --cov-context=test, then generates
# coverage_test_mapping.json. The mutation step reads pyproject.toml config.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

MAPPING_FILE="coverage_test_mapping.json"
COVERAGE_FILE=".coverage"

# Default markers to exclude (expensive / external / browser tests)
EXCLUDE_MARKERS="postgres or integration or llm_integration or playwright or gemini_live"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options] [mutant_pattern...]

Options:
  --build-map          Generate coverage data and test mapping
  --narrow MODULE      Narrow test selection using coverage map for MODULE
  --all-tests          Use full test suite (no narrowing)
  --debug              Enable mutmut debug output
  --max-children N     Max parallel mutant workers (default: CPU count)
  -h, --help           Show this help

Examples:
  $(basename "$0") --build-map
  $(basename "$0")                                    # run all mutants
  $(basename "$0") 'family_assistant.tools.*'         # pattern match mutants
  $(basename "$0") --narrow family_assistant.tools    # narrow tests for module
EOF
}

build_map() {
    echo "=== Building coverage-to-test mapping ==="
    echo ""

    # Run tests with coverage context tracking
    echo "Running tests with coverage context tracking..."
    pytest \
        --cov=src/family_assistant \
        --cov-context=test \
        --cov-report= \
        -m "not ($EXCLUDE_MARKERS)" \
        -q --no-header \
        --timeout=120 \
        -n0 \
        tests/unit/ tests/functional/ \
        || true  # Don't fail if some tests fail; we still want the coverage data

    echo ""
    echo "Building test mapping from coverage data..."
    python "$SCRIPT_DIR/build-coverage-map.py"
    echo ""
    echo "Done. You can now run mutation testing:"
    echo "  $(basename "$0")                                    # all mutants"
    echo "  $(basename "$0") --narrow family_assistant.tools    # narrowed tests"
}

narrow_tests_for_module() {
    local module="$1"

    if [[ ! -f "$MAPPING_FILE" ]]; then
        echo "Error: $MAPPING_FILE not found. Run --build-map first." >&2
        exit 1
    fi

    # Look up test paths for the module
    local test_paths
    test_paths=$(python3 -c "
import json, sys
with open('$MAPPING_FILE', encoding='utf-8') as f:
    mapping = json.load(f)

# Try exact match first, then prefix match
module = '$module'
if module in mapping:
    paths = mapping[module]
elif any(k.startswith(module) for k in mapping):
    # Union all matching entries
    paths = sorted(set(
        p for k, v in mapping.items()
        if k.startswith(module)
        for p in v
    ))
else:
    print(f'Warning: No coverage data for {module}', file=sys.stderr)
    sys.exit(1)

for p in paths:
    print(p)
")

    if [[ -z "$test_paths" ]]; then
        echo "Warning: No test paths found for $module. Using default test selection." >&2
        return 1
    fi

    echo "Narrowed test selection for $module:"
    echo "$test_paths" | sed 's/^/  /'
    echo ""

    # Build pytest test selection args as a TOML array
    local toml_array="["
    local first=true
    while IFS= read -r path; do
        if [[ "$first" == "true" ]]; then
            first=false
        else
            toml_array+=", "
        fi
        toml_array+="\"$path\""
    done <<< "$test_paths"

    # Add marker exclusions
    toml_array+=", \"-m\", \"not ($EXCLUDE_MARKERS)\""
    toml_array+="]"

    echo "$toml_array"
}

run_mutmut() {
    local mutant_args=("$@")
    local max_children_arg=()

    if [[ -n "${MAX_CHILDREN:-}" ]]; then
        max_children_arg=(--max-children "$MAX_CHILDREN")
    fi

    echo "=== Running mutation testing ==="
    echo ""

    if [[ ${#mutant_args[@]} -gt 0 ]]; then
        echo "Mutant pattern(s): ${mutant_args[*]}"
    else
        echo "Testing all mutants"
    fi
    echo ""

    mutmut run "${max_children_arg[@]}" "${mutant_args[@]}"
    local exit_code=$?

    echo ""
    echo "=== Results ==="
    mutmut results || true

    return $exit_code
}

# Parse arguments
BUILD_MAP=false
NARROW_MODULE=""
ALL_TESTS=false
DEBUG=false
MAX_CHILDREN=""
MUTANT_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build-map)
            BUILD_MAP=true
            shift
            ;;
        --narrow)
            NARROW_MODULE="$2"
            shift 2
            ;;
        --all-tests)
            ALL_TESTS=true
            shift
            ;;
        --debug)
            DEBUG=true
            shift
            ;;
        --max-children)
            MAX_CHILDREN="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            MUTANT_ARGS+=("$1")
            shift
            ;;
    esac
done

# Execute
if [[ "$BUILD_MAP" == "true" ]]; then
    build_map
    exit 0
fi

# If narrowing, update pyproject.toml temporarily
if [[ -n "$NARROW_MODULE" ]]; then
    test_selection=$(narrow_tests_for_module "$NARROW_MODULE")
    if [[ $? -eq 0 ]]; then
        # The last line of output is the TOML array
        toml_value=$(echo "$test_selection" | tail -1)

        # Back up and modify pyproject.toml
        cp pyproject.toml pyproject.toml.mutmut-backup
        trap 'mv pyproject.toml.mutmut-backup pyproject.toml' EXIT

        python3 -c "
import sys
content = open('pyproject.toml', encoding='utf-8').read()
# Replace the pytest_add_cli_args_test_selection line
import re
new_content = re.sub(
    r'pytest_add_cli_args_test_selection\s*=\s*\[.*?\]',
    'pytest_add_cli_args_test_selection = $toml_value',
    content,
    flags=re.DOTALL,
)
if new_content == content:
    print('Warning: Could not find pytest_add_cli_args_test_selection in pyproject.toml', file=sys.stderr)
    sys.exit(1)
open('pyproject.toml', 'w', encoding='utf-8').write(new_content)
"
        echo "Updated pyproject.toml with narrowed test selection"
        echo ""
    fi
fi

run_mutmut "${MUTANT_ARGS[@]}"

