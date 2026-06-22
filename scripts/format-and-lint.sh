#!/bin/bash

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

# Check for --fast flag (reserved for future use)
# FAST_MODE=0
if [ "$1" = "--fast" ]; then
    # FAST_MODE=1
    shift # Remove --fast from arguments
fi

VENV_BIN="${VIRTUAL_ENV:-.venv}/bin"
PYTHON_BIN="${VENV_BIN}/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

# Separate files by type
PYTHON_FILES=()
JS_TS_FILES=()
MARKDOWN_FILES=()
OTHER_FILES=()

# Function to categorize files
categorize_files() {
    for arg in "$@"; do
        if [ -d "$arg" ]; then
            # For directories, categorize them appropriately
            case "$arg" in
                frontend*|*frontend*)
                    if [ -d "frontend" ]; then
                        JS_TS_FILES+=("$arg")
                    fi
                    ;;
                *)
                    PYTHON_FILES+=("$arg")
                    ;;
            esac
        elif [ -f "$arg" ]; then
            case "$arg" in
                *.py) PYTHON_FILES+=("$arg") ;;
                *.js|*.jsx|*.ts|*.tsx|*.vue) JS_TS_FILES+=("$arg") ;;
                *.md) MARKDOWN_FILES+=("$arg") ;;
                *.sh|*.bash) OTHER_FILES+=("$arg") ;;
                *) OTHER_FILES+=("$arg") ;;
            esac
        elif [ -n "$arg" ]; then
            echo "Warning: File or directory not found: $arg"
        fi
    done
}

# Default to src and tests directories if no arguments provided
if [ $# -eq 0 ]; then
    PYTHON_FILES=("src" "tests")
    if [ -d "frontend" ]; then
        JS_TS_FILES=("frontend")
    fi
    # Find tracked markdown files. This avoids generated or vendored build
    # output such as ignored iOS DerivedData checkouts.
    if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        while IFS= read -r -d '' file; do
            MARKDOWN_FILES+=("$file")
        done < <(git ls-files -z -- "*.md" ":(exclude).claude/*")
    else
        while IFS= read -r -d '' file; do
            MARKDOWN_FILES+=("$file")
        done < <(find . -name "*.md" -not -path "./.venv/*" -not -path "./venv/*" -not -path "./.git/*" -not -path "*/node_modules/*" -not -path "./scratch/*" -not -path "./.claude/*" -not -path "*/build/*" -print0 2>/dev/null)
    fi
else
    categorize_files "$@"
fi

# Overall timer
OVERALL_START=$(date +%s)

echo "${BLUE}🚀 Running comprehensive format and lint checks...${NC}"
echo ""

HAS_ERRORS=0

# Phase 1: Python files (if any)
if [ ${#PYTHON_FILES[@]} -gt 0 ]; then
    echo "${BLUE}📝 Python files...${NC}"
    
    # Ruff check
    echo -n "${BLUE}  ▸ Running ruff check...${NC}"
    timer_start
    if ! "${VIRTUAL_ENV:-.venv}"/bin/ruff check --fix --ignore=E501 "${PYTHON_FILES[@]}" 2>&1; then
        timer_end
        echo ""
        echo "${YELLOW}💡 Showing suggested fixes (including unsafe ones):${NC}"
        "${VIRTUAL_ENV:-.venv}"/bin/ruff check --unsafe-fixes --diff --ignore=E501 "${PYTHON_FILES[@]}"
        echo ""
        echo "${RED}❌ ruff check failed. Fix the issues above and try again. Use ruff check --fix --unsafe-fixes to apply.${NC}"
        HAS_ERRORS=1
    else
        echo -n "${GREEN} ✓${NC}"
        timer_end
    fi
    
    # Ruff format
    if [ $HAS_ERRORS -eq 0 ]; then
        echo -n "${BLUE}  ▸ Running ruff format...${NC}"
        timer_start
        if ! "${VIRTUAL_ENV:-.venv}"/bin/ruff format "${PYTHON_FILES[@]}" 2>&1; then
            timer_end
            echo ""
            echo "${RED}❌ ruff format failed${NC}"
            HAS_ERRORS=1
        else
            echo -n "${GREEN} ✓${NC}"
            timer_end
        fi
    fi
    
    # Type checking
    if [ $HAS_ERRORS -eq 0 ]; then
        echo -n "${BLUE}  ▸ Running basedpyright...${NC}"
        timer_start
        if ! "${VIRTUAL_ENV:-.venv}"/bin/basedpyright "${PYTHON_FILES[@]}" 2>&1; then
            timer_end
            echo ""
            echo "${RED}❌ basedpyright type checking failed${NC}"
            HAS_ERRORS=1
        else
            echo -n "${GREEN} ✓${NC}"
            timer_end
        fi
    fi
    
    # Pylint (errors only)
    if [ $HAS_ERRORS -eq 0 ]; then
        echo -n "${BLUE}  ▸ Running pylint...${NC}"
        timer_start
        if ! "${VIRTUAL_ENV:-.venv}"/bin/pylint --errors-only "${PYTHON_FILES[@]}" 2>&1; then
            timer_end
            echo ""
            echo "${RED}❌ pylint found errors${NC}"
            HAS_ERRORS=1
        else
            echo -n "${GREEN} ✓${NC}"
            timer_end
        fi
    fi

    # Code conformance (ast-grep)
    if [ $HAS_ERRORS -eq 0 ]; then
        echo -n "${BLUE}  ▸ Running code conformance check...${NC}"
        timer_start
        if ! "$PYTHON_BIN" .ast-grep/check-conformance.py "${PYTHON_FILES[@]}" >/dev/null 2>&1; then
            timer_end
            echo ""
            echo "${RED}❌ Code conformance violations found${NC}"
            echo ""
            "$PYTHON_BIN" .ast-grep/check-conformance.py "${PYTHON_FILES[@]}"
            HAS_ERRORS=1
        else
            echo -n "${GREEN} ✓${NC}"
            timer_end
        fi
    fi

    echo ""
fi

# Phase 2: JavaScript/TypeScript files (if any)
if [ ${#JS_TS_FILES[@]} -gt 0 ]; then
    echo "${BLUE}🌐 Frontend JavaScript/TypeScript files...${NC}"
    
    # Biome format
    echo -n "${BLUE}  ▸ Running Biome format...${NC}"
    timer_start
    if ! npm run format --prefix frontend 2>&1; then
        timer_end
        echo ""
        echo "${RED}❌ Biome format failed${NC}"
        HAS_ERRORS=1
    else
        echo -n "${GREEN} ✓${NC}"
        timer_end
    fi
    
    # ESLint
    if [ $HAS_ERRORS -eq 0 ]; then
        echo -n "${BLUE}  ▸ Running ESLint...${NC}"
        timer_start
        if ! npm run lint:fix --prefix frontend 2>&1; then
            timer_end
            echo ""
            echo "${RED}❌ ESLint failed${NC}"
            HAS_ERRORS=1
        else
            echo -n "${GREEN} ✓${NC}"
            timer_end
        fi
    fi

    # TypeScript type checking
    if [ $HAS_ERRORS -eq 0 ]; then
        echo -n "${BLUE}  ▸ Running TypeScript type checking...${NC}"
        timer_start
        if ! npm run typecheck --prefix frontend 2>&1; then
            timer_end
            echo ""
            echo "${RED}❌ TypeScript type checking failed${NC}"
            HAS_ERRORS=1
        else
            echo -n "${GREEN} ✓${NC}"
            timer_end
        fi
    fi

    echo ""
fi

# Phase 3: Markdown files (if any)
if [ ${#MARKDOWN_FILES[@]} -gt 0 ]; then
    if command -v "${VIRTUAL_ENV:-.venv}"/bin/mdformat >/dev/null 2>&1; then
        echo "${BLUE}📄 Markdown files...${NC}"
        echo -n "${BLUE}  ▸ Running mdformat...${NC}"
        timer_start
        if "${VIRTUAL_ENV:-.venv}"/bin/mdformat --wrap 100 "${MARKDOWN_FILES[@]}" 2>/dev/null; then
            echo -n "${GREEN} ✓${NC}"
            timer_end
        else
            timer_end
            echo ""
            echo "${RED}❌ mdformat failed${NC}"
            HAS_ERRORS=1
        fi
        echo ""
    else
        echo "${RED}❌ mdformat not found. Please install dependencies.${NC}"
        HAS_ERRORS=1
        echo ""
    fi
fi

# Phase 4: Shell scripts and other files
if [ ${#OTHER_FILES[@]} -gt 0 ]; then
    echo "${BLUE}🔧 Other files (shell scripts, etc.)...${NC}"
    echo -n "${BLUE}  ▸ Checking syntax...${NC}"
    timer_start

    SHELL_ERRORS=0
    for file in "${OTHER_FILES[@]}"; do
        case "$file" in
            *review-hook.sh|*hook*.sh)
                # Special case for hook scripts that use bash features
                if ! bash -n "$file" 2>/dev/null; then
                    echo ""
                    echo "${RED}❌ Syntax error in bash script: $file${NC}"
                    bash -n "$file"
                    SHELL_ERRORS=1
                    HAS_ERRORS=1
                fi
                ;;
            *.sh|*.bash)
                if ! bash -n "$file" 2>/dev/null; then
                    echo ""
                    echo "${RED}❌ Syntax error in shell script: $file${NC}"
                    bash -n "$file"
                    SHELL_ERRORS=1
                    HAS_ERRORS=1
                fi
                ;;
            *)
                # For other files, just check if they're readable
                if [ ! -r "$file" ]; then
                    echo ""
                    echo "${RED}❌ Cannot read file: $file${NC}"
                    HAS_ERRORS=1
                fi
                ;;
        esac
    done

    if [ $SHELL_ERRORS -eq 0 ]; then
        echo -n "${GREEN} ✓${NC}"
    else
        echo -n "${RED} ✗${NC}"
    fi
    timer_end
    echo ""
fi

# Phase 5: Shellcheck for shell scripts
SHELL_FILES=()
if [ ${#OTHER_FILES[@]} -gt 0 ]; then
    for file in "${OTHER_FILES[@]}"; do
        case "$file" in
            *.sh|*.bash)
                SHELL_FILES+=("$file")
                ;;
        esac
    done
fi

# Also check for shell scripts if no specific files were provided
if [ $# -eq 0 ]; then
    while IFS= read -r -d '' file; do
        SHELL_FILES+=("$file")
    done < <(find scripts -name "*.sh" -type f -print0 2>/dev/null)
fi

if [ ${#SHELL_FILES[@]} -gt 0 ]; then
    # Find shellcheck - prefer .venv/bin, fallback to system
    SHELLCHECK_BIN=""
    if [ -x "${VIRTUAL_ENV:-.venv}/bin/shellcheck" ]; then
        SHELLCHECK_BIN="${VIRTUAL_ENV:-.venv}/bin/shellcheck"
    elif command -v shellcheck >/dev/null 2>&1; then
        SHELLCHECK_BIN="shellcheck"
    fi

    if [ -n "$SHELLCHECK_BIN" ]; then
        echo "${BLUE}🐚 Shell scripts (shellcheck)...${NC}"
        echo -n "${BLUE}  ▸ Running shellcheck...${NC}"
        timer_start

        if ! "$SHELLCHECK_BIN" -x --severity=warning "${SHELL_FILES[@]}" 2>&1; then
            timer_end
            echo ""
            echo "${RED}❌ shellcheck found issues${NC}"
            HAS_ERRORS=1
        else
            echo -n "${GREEN} ✓${NC}"
            timer_end
        fi
        echo ""
    else
        echo "${RED}❌ shellcheck not found, cannot lint shell scripts${NC}"
        echo "${RED}   Install with: ./scripts/install-shellcheck.sh${NC}"
        HAS_ERRORS=1
        echo ""
    fi
fi

# Summary
OVERALL_END=$(date +%s)
OVERALL_ELAPSED=$((OVERALL_END - OVERALL_START))

echo ""
if [ $HAS_ERRORS -eq 0 ]; then
    echo "${GREEN}✅ All format and lint checks passed! (${OVERALL_ELAPSED}s total)${NC}"
    exit 0
else
    echo "${RED}❌ Some format and lint checks failed. Please fix the issues above. (${OVERALL_ELAPSED}s total)${NC}"
    exit 1
fi
