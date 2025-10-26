#!/usr/bin/env python3
# /// script
# dependencies = [
#   "llm>=0.27",
#   "llm-gemini",
#   "llm-openrouter>=0.5",
# ]
# ///
"""
Enhanced code review script using LLM with tools for better context understanding.
Replaces the bash-based review-changes.sh with Python + llm library.

The CodeReviewToolbox class provides tools for reading files, searching patterns,
and submitting reviews. These tools are actively used during the review process.
The system uses a tool-based approach with a schema-based fallback for robustness.
"""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import llm

# Configure logger
logger = logging.getLogger(__name__)


class ReviewSubmittedException(Exception):
    """Raised when review is submitted to break out of tool chain."""

    pass


class CodeReviewToolbox(llm.Toolbox):
    """Tools for enhanced code review - read files and search patterns."""

    def __init__(self, repo_root: Path, max_file_size: int = 100_000) -> None:
        self.repo_root = repo_root.resolve()
        self.max_file_size = max_file_size
        self.review_submitted = False
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self.review_data: dict[str, Any] = {}

    def before_call(self, tool: object | None, tool_call: object) -> None:
        """
        Hook called before each tool execution. Logs the requested tool call.
        """
        requested_name = getattr(tool_call, "name", "unknown")
        provided_tool_name = getattr(tool, "name", None)
        arguments_repr = repr(getattr(tool_call, "arguments", {}))
        if len(arguments_repr) > 500:
            arguments_repr = f"{arguments_repr[:500]}...(truncated)..."

        if tool is None:
            logger.debug(
                "before_call hook: requested tool '%s' is not provided; arguments=%s",
                requested_name,
                arguments_repr,
            )
        else:
            logger.debug(
                "before_call hook: calling tool '%s' (requested='%s') with arguments=%s",
                provided_tool_name,
                requested_name,
                arguments_repr,
            )

    def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        """
        Read a file from the repository. Handles long files with line ranges.

        Args:
            path: Relative path to file within repository
            start_line: Optional starting line number (1-indexed)
            end_line: Optional ending line number (inclusive)

        Returns:
            File content or error message
        """
        logger.debug(
            f"Tool called: read_file(path={path}, start_line={start_line}, end_line={end_line})"
        )

        full_path = (self.repo_root / path).resolve()

        # Security: ensure path is within repo
        try:
            full_path.relative_to(self.repo_root)
        except ValueError:
            logger.debug(f"read_file failed: path outside repository: {path}")
            return f"ERROR: Path {path} is outside repository"

        if not full_path.exists():
            logger.debug(f"read_file failed: file not found: {path}")
            return f"ERROR: File {path} does not exist"

        # Check if binary
        try:
            # Size check
            file_size = full_path.stat().st_size
            if file_size > self.max_file_size and not start_line:
                return f"File too large ({file_size} bytes). Please specify line range with start_line and end_line."

            with open(full_path, encoding="utf-8") as f:
                if start_line:
                    lines = f.readlines()
                    start_idx = max(0, start_line - 1)
                    end_idx = end_line if end_line else len(lines)
                    return "".join(lines[start_idx:end_idx])
                return f.read(self.max_file_size)
        except UnicodeDecodeError:
            return f"ERROR: {path} appears to be a binary file"
        except Exception as e:
            return f"ERROR: Failed to read {path}: {e}"

    def search_pattern(
        self, pattern: str, file_glob: str | None = None, max_results: int = 30
    ) -> str:
        """
        Search for pattern using ripgrep. Respects .gitignore by default.

        Args:
            pattern: Pattern to search for (regex supported)
            file_glob: Optional glob pattern to filter files (e.g., "*.py")
            max_results: Maximum number of results to return

        Returns:
            Search results in format "file:line: content" or error message
        """
        logger.debug(
            f"Tool called: search_pattern(pattern={pattern!r}, file_glob={file_glob}, max_results={max_results})"
        )

        # SECURITY: Protect against ReDoS attacks
        MAX_PATTERN_LENGTH = 500
        if len(pattern) > MAX_PATTERN_LENGTH:
            return f"ERROR: Pattern too long (max {MAX_PATTERN_LENGTH} characters)"

        # Additional ReDoS protection: check for dangerous patterns
        dangerous_patterns = [
            r"(\w+)*",  # Excessive backtracking
            r"(\d+)+",  # Nested quantifiers
            r"(.*)+",  # Catastrophic backtracking
        ]
        for dangerous in dangerous_patterns:
            if dangerous in pattern:
                return "ERROR: Pattern contains potentially dangerous regex constructs"

        # SECURITY NOTE: subprocess.run() with list args is safe from command injection
        cmd = ["rg", "--json", "-m", str(max_results)]

        if file_glob:
            cmd.extend(["--glob", file_glob])

        cmd.append(pattern)

        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                timeout=5,
                text=True,
                check=True,
            )

            matches = []
            for line in result.stdout.splitlines():
                if line:
                    try:
                        data = json.loads(line)
                        if data.get("type") == "match":
                            match_data = data["data"]
                            path = match_data["path"]["text"]
                            line_no = match_data["line_number"]
                            text = match_data["lines"]["text"].strip()
                            matches.append(f"{path}:{line_no}: {text}")
                    except json.JSONDecodeError:
                        continue

            if not matches:
                return "No matches found"
            return "\n".join(matches[:max_results])

        except subprocess.TimeoutExpired:
            return "ERROR: Search timed out after 5 seconds"
        except subprocess.CalledProcessError as e:
            return f"ERROR: Search failed: {e}"
        except Exception as e:
            return f"ERROR: Unexpected error during search: {e}"

    def get_file_context(self, file: str, line: int, context_lines: int = 5) -> str:
        """
        Get context around a specific line number.

        Args:
            file: Path to file
            line: Line number to get context around
            context_lines: Number of lines before/after to include

        Returns:
            Context with line numbers and marker for target line
        """
        logger.debug(
            f"Tool called: get_file_context(file={file}, line={line}, context_lines={context_lines})"
        )

        start = max(1, line - context_lines)
        end = line + context_lines
        content = self.read_file(file, start_line=start, end_line=end)

        if content.startswith("ERROR"):
            return content

        lines = content.splitlines()
        result = []
        for i, line_content in enumerate(lines, start=start):
            marker = ">>> " if i == line else "    "
            result.append(f"{i:4d}{marker}{line_content}")
        return "\n".join(result)

    def submit_review(
        self,
        summary: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        issues: list[dict[str, Any]] | None = None,
        positive_aspects: list[str] | None = None,
    ) -> str:
        """
        Submit the final code review after analysis.

        Args:
            summary: One paragraph describing the changes
            issues: List of issues found, each with keys:
                - severity: One of BREAKS_BUILD, RUNTIME_ERROR, SECURITY_RISK,
                           LOGIC_ERROR, DESIGN_FLAW_MAJOR, DESIGN_FLAW_MINOR,
                           BEST_PRACTICE, STYLE, SUGGESTION
                - file: File path where issue was found
                - line: Optional line number
                - description: Clear description of the issue
                - suggestion: How to fix the issue
            positive_aspects: List of good practices observed

        Returns:
            Confirmation message
        """
        logger.debug(
            f"Tool called: submit_review(summary={summary[:50]}..., issues={len(issues or [])}, positive_aspects={len(positive_aspects or [])})"
        )

        # Prepare the review data for validation
        review_data = {
            "summary": summary,
            "issues": issues or [],
            "positive_aspects": positive_aspects or [],
        }

        # Validate issues structure BEFORE setting submission status
        for issue in review_data["issues"]:
            if not isinstance(issue, dict):
                return "ERROR: Each issue must be a dictionary/object"
            required = ["severity", "file", "description", "suggestion"]
            missing = [k for k in required if k not in issue]
            if missing:
                return f"ERROR: Issue missing required fields: {missing}"

        # Only set submission status after successful validation
        self.review_submitted = True
        self.review_data = review_data

        issue_count = len(self.review_data["issues"])
        return f"Review submitted successfully with {issue_count} issue(s) found."

    def after_call(self, tool: object, tool_call: object, tool_result: object) -> None:
        """
        Hook called after each tool execution.
        Raises ReviewSubmittedException when submit_review is called to exit the chain.
        """
        arguments_repr = repr(getattr(tool_call, "arguments", {}))
        if len(arguments_repr) > 500:
            arguments_repr = f"{arguments_repr[:500]}...(truncated)..."

        result_output = getattr(tool_result, "output", tool_result)
        result_output_repr = repr(result_output)
        if len(result_output_repr) > 500:
            result_output_repr = f"{result_output_repr[:500]}...(truncated)..."

        logger.debug(
            "after_call hook: tool=%s arguments=%s result=%s review_submitted=%s",
            getattr(tool, "name", "unknown"),
            arguments_repr,
            result_output_repr,
            self.review_submitted,
        )

        if getattr(tool, "name", None) == "submit_review" and self.review_submitted:
            logger.debug("Raising ReviewSubmittedException to exit tool chain")
            raise ReviewSubmittedException("Review submitted, exiting tool chain")


def get_diff(mode: str = "staged") -> str:
    """Get the git diff based on mode."""
    cmd = ["git", "diff", "--cached"] if mode == "staged" else ["git", "show", "HEAD"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return ""
    return result.stdout


def get_diff_stat(mode: str = "staged") -> str:
    """Get the git diff statistics."""
    cmd = (
        ["git", "diff", "--cached", "--stat"]
        if mode == "staged"
        else ["git", "show", "HEAD", "--stat"]
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return ""
    return result.stdout


def get_changed_files(mode: str = "staged") -> list[str]:
    """Get list of changed files."""
    cmd = (
        ["git", "diff", "--cached", "--name-only"]
        if mode == "staged"
        else ["git", "show", "HEAD", "--name-only", "--pretty=format:"]
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [f for f in result.stdout.splitlines() if f]


# Constants for smart truncation
EXTRA_CONTEXT_LINES = 5  # Lines to include after hunk header
HEADER_PREVIEW_LINES = 30  # Lines to check for header


def smart_truncate_diff(diff: str, max_chars: int = 50000) -> tuple[str, bool]:
    """
    Truncate diff intelligently - longest files first, exclude generated files.

    Returns:
        Tuple of (truncated_diff, was_truncated)
    """

    # Files to always exclude from detailed diff
    EXCLUDE_PATTERNS = {
        "uv.lock",
        "package-lock.json",
        "yarn.lock",
        "poetry.lock",
        "Pipfile.lock",
        "go.sum",
        "Cargo.lock",
        ".coverage",
        "pnpm-lock.yaml",
        "composer.lock",
        "Gemfile.lock",
    }

    # Extensions to exclude
    EXCLUDE_EXTENSIONS = {
        ".min.js",
        ".min.css",
        ".map",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
    }

    # Directories to exclude
    EXCLUDE_DIRS = {
        "dist/",
        "build/",
        "__pycache__/",
        ".next/",
        ".nuxt/",
        "node_modules/",
    }

    if len(diff) <= max_chars:
        return diff, False

    # Parse diff into file sections
    file_sections = []
    current_file = None
    current_content = []

    for line in diff.splitlines():
        if line.startswith("diff --git"):
            if current_file:
                file_sections.append((current_file, "\n".join(current_content)))
            # Extract filename
            parts = line.split()
            if len(parts) >= 4:
                current_file = parts[2][2:] if parts[2].startswith("a/") else parts[2]
            current_content = [line]
        elif current_content is not None:
            current_content.append(line)

    if current_file:
        file_sections.append((current_file, "\n".join(current_content)))

    # Filter out excluded files
    filtered_sections = []
    excluded_files = []

    for filename, content in file_sections:
        should_exclude = (
            filename in EXCLUDE_PATTERNS
            or any(filename.endswith(ext) for ext in EXCLUDE_EXTENSIONS)
            or any(dir_pattern in filename for dir_pattern in EXCLUDE_DIRS)
        )

        if should_exclude:
            excluded_files.append(filename)
        else:
            filtered_sections.append((filename, content))

    # Sort by size (longest first) for truncation
    filtered_sections.sort(key=lambda x: len(x[1]), reverse=True)

    # Build truncated diff
    result = []
    current_size = 0
    truncated_files = []

    for filename, content in filtered_sections:
        if current_size + len(content) > max_chars:
            # Try to include at least the file header
            header_lines = []
            for line in content.splitlines()[:HEADER_PREVIEW_LINES]:
                header_lines.append(line)
                if line.startswith("@@"):  # Include up to first hunk header
                    # Try to include a few lines after the hunk header
                    for extra_line in content.splitlines()[
                        len(header_lines) : len(header_lines) + EXTRA_CONTEXT_LINES
                    ]:
                        header_lines.append(extra_line)
                    break

            partial = "\n".join(header_lines)
            if current_size + len(partial) < max_chars:
                result.append(partial)
                result.append(
                    f"\n[... {filename} truncated - {len(content)} chars total ...]"
                )
                truncated_files.append(filename)
                current_size += len(partial) + 50  # Account for truncation message
            else:
                truncated_files.append(filename)
                break  # Can't fit even the header
        else:
            result.append(content)
            current_size += len(content)

    # Add summary of what was excluded/truncated
    summary = []
    if excluded_files:
        summary.append(
            f"\n[Generated/lock files excluded from diff: {', '.join(excluded_files)}]"
        )
    if truncated_files:
        summary.append(f"[Files truncated due to size: {', '.join(truncated_files)}]")
        summary.append("[Use the read_file tool to examine truncated files in detail]")
    if summary:
        result.append("\n".join(summary))

    return "\n".join(result), True


def get_baseline_commit(mode: str = "staged") -> str:
    """Get the baseline commit for the diff."""
    if mode == "staged":
        # For staged changes, baseline is HEAD
        cmd = ["git", "rev-parse", "HEAD"]
    else:
        # For commit mode, baseline is the parent of HEAD
        cmd = ["git", "rev-parse", "HEAD~1"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def compute_cache_key(diff: str, baseline_commit: str) -> str:
    """
    Compute cache key based on diff content and baseline commit.

    Args:
        diff: The git diff content
        baseline_commit: The baseline commit hash

    Returns:
        SHA256 hash as hex string
    """
    cache_input = f"{baseline_commit}\n{diff}".encode()
    return hashlib.sha256(cache_input).hexdigest()


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def get_cached_review(cache_key: str, cache_dir: Path) -> dict[str, Any] | None:
    """
    Read cached review if available.

    Args:
        cache_key: The cache key (hash)
        cache_dir: Directory where cache files are stored

    Returns:
        Cached review data or None if not found/invalid
    """
    cache_file = cache_dir / f"{cache_key}.json"

    if not cache_file.exists():
        return None

    try:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Invalid cache file, ignore it
        return None


def save_cached_review(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    cache_key: str,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    review_data: dict[str, Any],
    cache_dir: Path,
) -> None:
    """
    Save review to cache.

    Args:
        cache_key: The cache key (hash)
        review_data: The review data to cache
        cache_dir: Directory where cache files are stored
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{cache_key}.json"

    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, indent=2)
    except OSError as e:
        # Cache write failure is not critical, just log it
        print(f"Warning: Failed to write cache: {e}", file=sys.stderr)


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def determine_exit_code(issues: list[dict[str, Any]]) -> tuple[int, str]:
    """Determine exit code and highest severity from issues."""
    exit_code = 0
    highest_severity = ""

    # Check for blocking issues (exit code 2)
    blocking_severities = [
        "BREAKS_BUILD",
        "RUNTIME_ERROR",
        "SECURITY_RISK",
        "LOGIC_ERROR",
        "DESIGN_FLAW_MAJOR",
    ]
    for issue in issues:
        if issue.get("severity") in blocking_severities:
            return 2, str(issue.get("severity", ""))

    # Check for warning issues (exit code 1)
    warning_severities = ["DESIGN_FLAW_MINOR", "BEST_PRACTICE"]
    for issue in issues:
        if issue.get("severity") in warning_severities:
            exit_code = 1
            highest_severity = str(issue.get("severity", ""))

    return exit_code, highest_severity


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def format_human_output(review_data: dict[str, Any], exit_code: int) -> None:
    """Format and print human-readable output."""
    # Colors
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    CYAN = "\033[0;36m"
    BOLD = "\033[1m"
    NC = "\033[0m"  # No Color

    print(f"\n{BLUE}🔍 Code Review Results{NC}", file=sys.stderr)
    print(f"\n{BOLD}Summary:{NC}", file=sys.stderr)
    print(review_data.get("summary", "No summary available"), file=sys.stderr)

    issues = review_data.get("issues", [])
    if issues:
        print(f"\n{BOLD}Issues Found:{NC}", file=sys.stderr)

        # Group by severity
        severity_colors = {
            "BREAKS_BUILD": RED,
            "RUNTIME_ERROR": RED,
            "SECURITY_RISK": RED,
            "LOGIC_ERROR": RED,
            "DESIGN_FLAW_MAJOR": RED,
            "DESIGN_FLAW_MINOR": YELLOW,
            "BEST_PRACTICE": YELLOW,
            "STYLE": CYAN,
            "SUGGESTION": CYAN,
        }

        for severity, color in severity_colors.items():
            severity_issues = [i for i in issues if i.get("severity") == severity]
            if severity_issues:
                print(f"\n{color}{BOLD}{severity}:{NC}", file=sys.stderr)
                for issue in severity_issues:
                    file_path = issue.get("file", "unknown")
                    line = issue.get("line")
                    location = f"{file_path}:{line}" if line else file_path
                    print(f"  {BOLD}{location}{NC}", file=sys.stderr)
                    print(
                        f"    {color}Issue:{NC} {issue.get('description', '')}",
                        file=sys.stderr,
                    )
                    print(
                        f"    {GREEN}Fix:{NC} {issue.get('suggestion', '')}",
                        file=sys.stderr,
                    )
    else:
        print(f"\n{GREEN}✓ No issues found!{NC}", file=sys.stderr)

    positive = review_data.get("positive_aspects", [])
    if positive:
        print(f"\n{GREEN}{BOLD}Positive Aspects:{NC}", file=sys.stderr)
        for aspect in positive:
            print(f"  {GREEN}✓{NC} {aspect}", file=sys.stderr)

    # Final verdict
    print(f"\n{BOLD}Review Result:{NC}", file=sys.stderr)
    if exit_code == 0:
        print(f"{GREEN}✓ All checks passed!{NC}", file=sys.stderr)
    elif exit_code == 1:
        print(
            f"{YELLOW}⚠ Minor issues found (highest: {review_data.get('highest_severity', 'UNKNOWN')}){NC}",
            file=sys.stderr,
        )
        print("Consider addressing these before merging.", file=sys.stderr)
    else:
        print(
            f"{RED}✗ Blocking issues found (highest: {review_data.get('highest_severity', 'UNKNOWN')}){NC}",
            file=sys.stderr,
        )
        print("These must be fixed before the code can be merged.", file=sys.stderr)

    print(f"\nExit code: {exit_code}", file=sys.stderr)


def review_changes(
    mode: str = "staged",
    output_json: bool = False,
    model_name: str | None = None,
    command: str | None = None,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
) -> tuple[int, dict[str, Any]]:
    """
    Main review function using LLM with tools.

    Args:
        mode: "staged" or "commit"
        output_json: Whether to output JSON instead of human-readable format
        model_name: Optional model name to use (e.g., 'gpt-4o', 'claude-3.5-sonnet')
        command: Optional git command being executed (for context)

    Returns:
        Tuple of (exit_code, review_data)
    """

    # Default to a fast, cost-effective model if available
    # Note: llm-openrouter plugin doesn't expose models through standard llm.get_models()
    # For now, rely on llm's default model configuration (gpt-4o-mini)
    # TODO: Figure out correct way to use OpenRouter models via llm Python API

    # Get repo root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as err:
        error_message = err.stderr.strip() if err.stderr else "Not in a git repository"
        print(f"Error: {error_message}", file=sys.stderr)
        sys.exit(1)

    repo_root = Path(result.stdout.strip())

    # Check for required files
    if not (repo_root / "REVIEW_GUIDELINES.md").exists():
        print(
            "Error: REVIEW_GUIDELINES.md not found in repository root", file=sys.stderr
        )
        sys.exit(1)

    # Redirect stdout to stderr for human output if JSON mode
    if output_json:
        original_stdout = sys.stdout
        sys.stdout = sys.stderr

    # Get diff and check if there are changes
    diff = get_diff(mode)
    if not diff.strip():
        print(
            f"No {'staged' if mode == 'staged' else 'committed'} changes to review",
            file=sys.stderr,
        )
        if output_json:
            sys.stdout = original_stdout
            print(
                json.dumps({
                    "summary": "No changes to review",
                    "issues": [],
                    "positive_aspects": [],
                    "exit_code": 0,
                })
            )
        return 0, {}

    # Get diff statistics and file list
    diff_stat = get_diff_stat(mode)
    changed_files = get_changed_files(mode)

    print(f"Reviewing {mode} changes...", file=sys.stderr)
    print("\nChange Statistics:", file=sys.stderr)
    print(diff_stat, file=sys.stderr)

    # Smart truncation
    truncated_diff, was_truncated = smart_truncate_diff(diff)

    if was_truncated:
        print(
            "\nNote: Diff was truncated due to size. LLM can use tools to examine files.",
            file=sys.stderr,
        )

    # Build prompt
    prompt_parts = []
    prompt_parts.append(
        "Review the following git diff according to the provided guidelines."
    )
    prompt_parts.append(
        "Tests and linting have already passed, so focus on logic, security, and design issues."
    )
    prompt_parts.append(
        "CRITICAL: Read the 'Understanding Context' section in guidelines."
    )
    prompt_parts.append(
        "\nIMPORTANT: After reviewing the changes, you MUST call the submit_review() tool "
        "to submit your findings. The tool takes three parameters:\n"
        "- summary: A paragraph describing the changes\n"
        "- issues: A list of issue objects (or empty list if no issues)\n"
        "- positive_aspects: A list of strings describing good practices (or empty list)\n"
    )

    if command:
        prompt_parts.append(f"\nGit Command:\n{command}")
        prompt_parts.append(
            "\nThis shows the git command being executed, which includes the commit message and any flags."
        )

    if was_truncated:
        prompt_parts.append("\nNOTE: The diff has been truncated to fit size limits.")
        prompt_parts.append(f"Changed files: {', '.join(changed_files)}")
        prompt_parts.append(
            "You can use read_file, search_pattern, and get_file_context tools to examine "
            "specific files or patterns if you need more context before submitting your review."
        )

    prompt_parts.append(f"\nDIFF:\n{truncated_diff}")
    prompt_parts.append(
        "\nRemember to call submit_review() with your findings after analyzing the changes."
    )

    prompt = "\n".join(prompt_parts)

    # Load guidelines
    with open(repo_root / "REVIEW_GUIDELINES.md", encoding="utf-8") as f:
        guidelines = f.read()

    # Load CLAUDE.md if exists
    claude_context = ""
    if (repo_root / "CLAUDE.md").exists():
        with open(repo_root / "CLAUDE.md", encoding="utf-8") as f:
            claude_context = f.read()

    # System prompt
    system = f"""You are a code reviewer analyzing git diffs. Review according to these guidelines:

{guidelines}

{"Project-specific context from CLAUDE.md:" if claude_context else ""}
{claude_context if claude_context else ""}

Key points to remember:
- Tests and linting have already passed, so don't flag formatting or basic syntax issues
- Focus on logic errors, security issues, design problems, and potential runtime failures
- Be constructive and specific in your feedback
- Read the clarifications and notes in the guidelines carefully - they explain what is NOT an issue
- Code prepared for future use with clear documentation is intentional, not a flaw
- Path validation using relative_to() or similar IS proper validation
- subprocess.run() with list arguments (not shell=True) is safe from injection
- If a git command is provided, use it to understand the developer's stated intent in the commit message
- Check if changes align with what the commit message describes

TOOLS AVAILABLE:
- read_file(path, start_line, end_line): Read file contents or specific lines
- search_pattern(pattern, file_glob, max_results): Search for patterns using ripgrep
- get_file_context(file, line, context_lines): Get context around a specific line
- submit_review(summary, issues, positive_aspects): Submit your final review (REQUIRED)

You MUST call submit_review() after analyzing the changes. Example:
submit_review(
    summary="The changes add a new feature...",
    issues=[
        {{
            "severity": "LOGIC_ERROR",
            "file": "src/main.py",
            "line": 42,
            "description": "Variable used before initialization",
            "suggestion": "Initialize the variable before use"
        }}
    ],
    positive_aspects=["Good error handling", "Well-documented functions"]
)"""

    # Define schema for structured output
    schema = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One paragraph describing the changes",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": [
                                "BREAKS_BUILD",
                                "RUNTIME_ERROR",
                                "SECURITY_RISK",
                                "LOGIC_ERROR",
                                "DESIGN_FLAW_MAJOR",
                                "DESIGN_FLAW_MINOR",
                                "BEST_PRACTICE",
                                "STYLE",
                                "SUGGESTION",
                            ],
                            "description": "Issue severity level",
                        },
                        "file": {
                            "type": "string",
                            "description": "File path where issue was found",
                        },
                        "line": {
                            "type": "integer",
                            "description": "Line number (optional)",
                        },
                        "description": {
                            "type": "string",
                            "description": "Clear description of the issue",
                        },
                        "suggestion": {
                            "type": "string",
                            "description": "How to fix the issue",
                        },
                    },
                    "required": ["severity", "file", "description", "suggestion"],
                },
                "description": "List of issues found",
            },
            "positive_aspects": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Good practices observed in the changes",
            },
        },
        "required": ["summary", "issues", "positive_aspects"],
    }

    # Check cache first
    cache_dir = repo_root / ".review_cache"
    baseline_commit = get_baseline_commit(mode)
    cache_key = compute_cache_key(diff, baseline_commit)

    cached_review = get_cached_review(cache_key, cache_dir)
    if cached_review:
        logger.debug(f"Cache hit: {cache_key}")
        print("Using cached review result.", file=sys.stderr)
        review_data = cached_review
    else:
        logger.debug(f"Cache miss: {cache_key}")
        print("\nAnalyzing changes with LLM...", file=sys.stderr)

        # Create toolbox with tools
        toolbox = CodeReviewToolbox(repo_root)
        tools = [
            toolbox.read_file,
            toolbox.search_pattern,
            toolbox.get_file_context,
            toolbox.submit_review,
        ]

        # Get the default configured model or the specified model
        model = llm.get_model(model_name) if model_name else llm.get_model()

        review_data = None

        # Create a conversation so we can send follow-up instructions if needed
        conversation = model.conversation(
            tools=tools,
            before_call=toolbox.before_call,
            after_call=toolbox.after_call,
        )

        def run_chain(message: str, label: str, include_system: bool = False) -> None:
            nonlocal review_data
            try:
                logger.debug("Running conversation chain (%s)", label)
                response = conversation.chain(
                    message,
                    system=system if include_system else None,
                )
                logger.debug("Fetching response text for (%s)", label)
                response.text()
            except ReviewSubmittedException:
                logger.debug("Caught ReviewSubmittedException (%s)", label)
            except Exception as e:
                logger.error(f"Error during {label}: {e}", exc_info=True)
                print(f"Error during review ({label}): {e}", file=sys.stderr)
                return
            finally:
                if toolbox.review_submitted and not review_data:
                    logger.debug("Review submitted via tool during %s", label)
                    review_data = toolbox.review_data
                    print("Review successfully submitted via tool.", file=sys.stderr)

        # First attempt with full prompt
        run_chain(prompt, "initial_prompt", include_system=True)

        # If the model did not call submit_review, try a follow-up instruction
        if not review_data:
            logger.debug(
                "Model did not submit review via tool; sending follow-up instruction"
            )
            print(
                "Model did not submit review via tool; sending follow-up instruction...",
                file=sys.stderr,
            )
            follow_up_prompt = (
                "Reminder: You must now call the submit_review(summary, issues, positive_aspects) "
                "tool to record your findings. Use empty lists when there are no issues or positive "
                "aspects. Do not provide a normal reply—call submit_review immediately."
            )
            run_chain(follow_up_prompt, "follow_up_prompt")

        if not review_data:
            logger.debug("Model still did not submit review after follow-up")

        # Fallback: Use schema without tools if no review submitted
        if not review_data:
            logger.debug("Falling back to schema-based review (no tools)")
            print(
                "\nFalling back to schema-based review (no tools)...", file=sys.stderr
            )
            try:
                logger.debug("Calling model.prompt() with schema")
                response = model.prompt(prompt, system=system, schema=schema)
                response_text = (
                    response.text() if hasattr(response, "text") else str(response)
                )

                if response_text:
                    logger.debug(
                        f"Schema-based review completed, parsing JSON ({len(response_text)} chars)"
                    )
                    review_data = json.loads(response_text)
                else:
                    logger.error("Empty response from LLM with schema")
                    print("Error: Empty response from LLM", file=sys.stderr)
                    if output_json:
                        sys.stdout = original_stdout
                    return 1, {}

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse LLM response as JSON: {e}")
                print(
                    f"Error: Failed to parse LLM response as JSON: {e}", file=sys.stderr
                )
                print(f"Response was: {response_text[:500]}", file=sys.stderr)
                if output_json:
                    sys.stdout = original_stdout
                return 1, {}
            except Exception as e:
                logger.error(f"Error calling LLM with schema: {e}", exc_info=True)
                print(f"Error calling LLM: {e}", file=sys.stderr)
                if output_json:
                    sys.stdout = original_stdout
                return 1, {}

        # Save to cache if we got a review
        if review_data:
            logger.debug(f"Saving review to cache: {cache_key}")
            save_cached_review(cache_key, review_data, cache_dir)

    # Determine exit code
    exit_code, highest_severity = determine_exit_code(review_data.get("issues", []))

    # Add metadata
    review_data["exit_code"] = exit_code
    review_data["highest_severity"] = highest_severity
    review_data["cache_key"] = cache_key

    # Output results
    if output_json:
        sys.stdout = original_stdout
        print(json.dumps(review_data))
    else:
        format_human_output(review_data, exit_code)

    return exit_code, review_data


def main() -> None:
    """Main entry point with argparse for robust argument handling."""
    parser = argparse.ArgumentParser(
        description="Review code changes using LLM with tools to identify potential issues.",
        epilog="""
Exit codes:
  0 - No issues or only style/suggestions
  1 - Minor issues (best practices, minor design flaws)
  2 - Major issues (build breaks, runtime errors, security risks)
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--commit",
        action="store_true",
        help="Review the most recent commit (default: review staged changes)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON instead of human-readable format",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging for tool calls and internal state",
    )
    parser.add_argument(
        "--model",
        type=str,
        help="LLM model to use for review (e.g., 'gpt-4o', 'claude-3.5-sonnet')",
    )
    parser.add_argument(
        "--command",
        type=str,
        help="Git command being executed (for context, typically includes commit message)",
    )

    args = parser.parse_args()

    # Configure logging based on --debug flag
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="[%(levelname)s] %(name)s: %(message)s",
            stream=sys.stderr,
        )
        logger.debug("Debug logging enabled")
    else:
        logging.basicConfig(
            level=logging.WARNING,
            format="[%(levelname)s] %(message)s",
            stream=sys.stderr,
        )

    mode = "commit" if args.commit else "staged"
    output_json = args.json
    model_name = args.model
    command = args.command

    try:
        exit_code, _ = review_changes(mode, output_json, model_name, command)
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nReview cancelled by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
