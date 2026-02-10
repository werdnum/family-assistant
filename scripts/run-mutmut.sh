#!/usr/bin/env bash
set -euo pipefail

# Mutation testing wrapper for mutmut v3.
#
# Usage:
#   scripts/run-mutmut.sh --build-map                     # Step 1: generate coverage + test mapping
#   scripts/run-mutmut.sh [mutant_pattern]                # Step 2: run mutmut (optionally with pattern)
#   scripts/run-mutmut.sh --narrow family_assistant.tools  # Step 2: run with narrowed test selection
#   scripts/run-mutmut.sh --changed [BASE_REF]            # Run on changed files vs base ref
#
# Options:
#   --build-map        Generate coverage data and test mapping (run first)
#   --narrow MODULE    Use coverage map to narrow test selection for MODULE
#   --changed [REF]    Run on files changed vs REF (default: origin/main)
#   --ci-summary       Write machine-readable JSON summary to mutmut-summary.json
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
  --changed [REF]      Run on files changed vs REF (default: origin/main)
  --ci-summary         Write JSON summary to mutmut-summary.json
  --all-tests          Use full test suite (no narrowing)
  --debug              Enable mutmut debug output
  --max-children N     Max parallel mutant workers (default: CPU count)
  -h, --help           Show this help

Examples:
  $(basename "$0") --build-map
  $(basename "$0")                                    # run all mutants
  $(basename "$0") 'family_assistant.tools.*'         # pattern match mutants
  $(basename "$0") --narrow family_assistant.tools    # narrow tests for module
  $(basename "$0") --changed                          # test changed files vs origin/main
  $(basename "$0") --changed origin/feature-branch    # test changed files vs specific ref
  $(basename "$0") --ci-summary --narrow family_assistant.utils  # with JSON output
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

# Get list of do_not_mutate files from pyproject.toml
get_do_not_mutate_files() {
    python3 -c "
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml', 'rb') as f:
    config = tomllib.load(f)
for path in config.get('tool', {}).get('mutmut', {}).get('do_not_mutate', []):
    print(path)
"
}

# Find changed Python source files and run mutation testing on them
run_changed() {
    local base_ref="$1"

    echo "=== Running mutation testing on changed files ==="
    echo "Base ref: $base_ref"
    echo ""

    # Get changed Python source files
    local changed_files
    changed_files=$(git diff --name-only "$base_ref"...HEAD -- 'src/family_assistant/**/*.py' 2>/dev/null || \
                    git diff --name-only "$base_ref" -- 'src/family_assistant/**/*.py')

    if [[ -z "$changed_files" ]]; then
        echo "No Python source files changed vs $base_ref"
        exit 0
    fi

    # Filter out do_not_mutate files
    local do_not_mutate
    do_not_mutate=$(get_do_not_mutate_files)

    local filtered_files=""
    while IFS= read -r file; do
        local skip=false
        while IFS= read -r excluded; do
            if [[ -n "$excluded" && "$file" == "$excluded" ]]; then
                skip=true
                break
            fi
        done <<< "$do_not_mutate"
        if [[ "$skip" == "false" ]]; then
            filtered_files+="$file"$'\n'
        else
            echo "  Skipping (do_not_mutate): $file"
        fi
    done <<< "$changed_files"

    # Remove trailing newline
    filtered_files=$(echo "$filtered_files" | sed '/^$/d')

    if [[ -z "$filtered_files" ]]; then
        echo "All changed files are in do_not_mutate list. Nothing to test."
        exit 0
    fi

    echo "Changed source files:"
    echo "$filtered_files" | sed 's/^/  /'
    echo ""

    # Convert file paths to dotted module names for mutant patterns
    local mutant_patterns=()
    local modules_for_narrow=()
    while IFS= read -r file; do
        # src/family_assistant/tools/notes.py -> family_assistant.tools.notes
        local module
        module=$(echo "$file" | sed 's|^src/||; s|\.py$||; s|/__init__$||; s|/|.|g')
        mutant_patterns+=("${module}.*")

        # Extract the top-level submodule for narrowing (e.g. family_assistant.tools)
        local parts
        IFS='.' read -ra parts <<< "$module"
        if [[ ${#parts[@]} -ge 2 ]]; then
            modules_for_narrow+=("${parts[0]}.${parts[1]}")
        else
            modules_for_narrow+=("${parts[0]}")
        fi
    done <<< "$filtered_files"

    # Deduplicate narrow modules
    local unique_narrow_modules
    unique_narrow_modules=$(printf '%s\n' "${modules_for_narrow[@]}" | sort -u)

    # Determine narrowing strategy
    local narrow_count
    narrow_count=$(echo "$unique_narrow_modules" | wc -l)

    if [[ "$narrow_count" -le 3 ]]; then
        # Few modules changed — narrow to their union
        # Build the coverage map first if it doesn't exist
        if [[ ! -f "$MAPPING_FILE" ]]; then
            echo "Building coverage map for test narrowing..."
            build_map
        fi

        # Collect union of test paths across all changed modules
        local all_test_paths=""
        local narrow_failures=0
        while IFS= read -r mod; do
            local mod_tests
            mod_tests=$(narrow_tests_for_module "$mod" 2>&1 | head -n -1 | tail -n +2 | sed 's/^  //' || true)
            if [[ -n "$mod_tests" ]]; then
                all_test_paths+="$mod_tests"$'\n'
            else
                echo "Warning: Could not narrow tests for $mod. Will use full test suite for safety." >&2
                narrow_failures=$((narrow_failures + 1))
            fi
        done <<< "$unique_narrow_modules"

        # If any narrowing failed, fall back to full test suite to avoid false survivors
        if [[ "$narrow_failures" -gt 0 ]]; then
            echo "Falling back to full test suite due to narrowing failures."
            echo ""
            all_test_paths=""
        fi

        if [[ -n "$all_test_paths" ]]; then
            # Deduplicate and create TOML array
            local deduped
            deduped=$(echo "$all_test_paths" | sed '/^$/d' | sort -u)

            echo "Narrowed test selection for changed modules:"
            echo "$deduped" | sed 's/^/  /'
            echo ""

            local toml_array="["
            local first=true
            while IFS= read -r path; do
                if [[ "$first" == "true" ]]; then
                    first=false
                else
                    toml_array+=", "
                fi
                toml_array+="\"$path\""
            done <<< "$deduped"
            toml_array+=", \"-m\", \"not ($EXCLUDE_MARKERS)\""
            toml_array+="]"

            # Temporarily update pyproject.toml
            cp pyproject.toml pyproject.toml.mutmut-backup
            trap 'mv pyproject.toml.mutmut-backup pyproject.toml' EXIT

            python3 -c "
import sys, re
content = open('pyproject.toml', encoding='utf-8').read()
new_content = re.sub(
    r'pytest_add_cli_args_test_selection\s*=\s*\[.*?\]',
    'pytest_add_cli_args_test_selection = $toml_array',
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
    else
        echo "Many modules changed ($narrow_count). Using full test suite."
        echo ""
    fi

    # Build mutant pattern string — join with comma
    local pattern
    pattern=$(printf '%s,' "${mutant_patterns[@]}")
    pattern="${pattern%,}"  # Remove trailing comma

    echo "Mutant pattern: $pattern"
    echo ""

    MUTANT_ARGS=("$pattern")
    run_mutmut "${MUTANT_ARGS[@]}"
}

write_ci_summary() {
    local module_name="${1:-all}"
    local summary_file="mutmut-summary.json"

    python3 -c "
import json, subprocess, sys

result = subprocess.run(['mutmut', 'results'], capture_output=True, text=True)
output = result.stdout + result.stderr

# Parse mutmut results output
total = killed = survived = no_tests = timeout = suspicious = 0
for line in output.splitlines():
    line = line.strip()
    if line.startswith('Killed'):
        killed = int(line.split(':')[0].split()[-1]) if ':' in line else 0
        # Count entries after colon
        parts = line.split(':', 1)
        if len(parts) > 1 and parts[1].strip():
            killed = len([x.strip() for x in parts[1].split(',') if x.strip()])
    elif line.startswith('Survived'):
        parts = line.split(':', 1)
        if len(parts) > 1 and parts[1].strip():
            survived = len([x.strip() for x in parts[1].split(',') if x.strip()])
    elif line.startswith('Untested') or 'no tests' in line.lower():
        parts = line.split(':', 1)
        if len(parts) > 1 and parts[1].strip():
            no_tests = len([x.strip() for x in parts[1].split(',') if x.strip()])
    elif line.startswith('Timeout'):
        parts = line.split(':', 1)
        if len(parts) > 1 and parts[1].strip():
            timeout = len([x.strip() for x in parts[1].split(',') if x.strip()])
    elif line.startswith('Suspicious'):
        parts = line.split(':', 1)
        if len(parts) > 1 and parts[1].strip():
            suspicious = len([x.strip() for x in parts[1].split(',') if x.strip()])

total = killed + survived + no_tests + timeout + suspicious
score = (killed / total * 100) if total > 0 else 0

summary = {
    'module': '$module_name',
    'total': total,
    'killed': killed,
    'survived': survived,
    'no_tests': no_tests,
    'timeout': timeout,
    'suspicious': suspicious,
    'score': round(score, 1),
}

with open('$summary_file', 'w') as f:
    json.dump(summary, f, indent=2)

print(json.dumps(summary, indent=2))
"

    echo ""
    echo "Summary written to $summary_file"
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
CI_SUMMARY=false
CHANGED_MODE=false
CHANGED_BASE_REF="origin/main"
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
        --changed)
            CHANGED_MODE=true
            shift
            # Optional base ref argument (if next arg doesn't start with --)
            if [[ $# -gt 0 && ! "$1" =~ ^-- ]]; then
                CHANGED_BASE_REF="$1"
                shift
            fi
            ;;
        --ci-summary)
            CI_SUMMARY=true
            shift
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

if [[ "$CHANGED_MODE" == "true" ]]; then
    run_changed "$CHANGED_BASE_REF"
    if [[ "$CI_SUMMARY" == "true" ]]; then
        write_ci_summary "changed"
    fi
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

if [[ "$CI_SUMMARY" == "true" ]]; then
    summary_module="${NARROW_MODULE:-all}"
    # If we have mutant args, derive module name from them
    if [[ ${#MUTANT_ARGS[@]} -gt 0 && "$summary_module" == "all" ]]; then
        summary_module="${MUTANT_ARGS[0]}"
    fi
    write_ci_summary "$summary_module"
fi

