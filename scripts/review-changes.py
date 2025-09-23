#!/usr/bin/env python3
"""
Enhanced code review script using LLM with tools for better context understanding.
Replaces the bash-based review-changes.sh with Python + llm library.

The CodeReviewToolbox class provides tools for reading files, searching patterns,
and submitting reviews. These tools are actively used during the review process.
The system uses a tool-based approach with a schema-based fallback for robustness.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import llm


class CodeReviewToolbox(llm.Toolbox):
    """Tools for enhanced code review - read files and search patterns."""

    def __init__(self, repo_root: Path, max_file_size: int = 100_000) -> None:
        self.repo_root = repo_root.resolve()
        self.max_file_size = max_file_size
        self.review_submitted = False
        self.review_data: dict[str, Any] = {}

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
        full_path = (self.repo_root / path).resolve()

        # Security: ensure path is within repo
        try:
            full_path.relative_to(self.repo_root)
        except ValueError:
            return f"ERROR: Path {path} is outside repository"

        if not full_path.exists():
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
    mode: str = "staged", output_json: bool = False
) -> tuple[int, dict[str, Any]]:
    """
    Main review function using LLM with tools.

    Args:
        mode: "staged" or "commit"
        output_json: Whether to output JSON instead of human-readable format

    Returns:
        Tuple of (exit_code, review_data)
    """

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

    print("\nAnalyzing changes with LLM...", file=sys.stderr)

    # Create toolbox with tools
    toolbox = CodeReviewToolbox(repo_root)
    tools = [
        toolbox.read_file,
        toolbox.search_pattern,
        toolbox.get_file_context,
        toolbox.submit_review,
    ]

    # Get the default configured model
    model = llm.get_model()

    review_data = None
    max_attempts = 3

    # Try to get review with tools (with retries)
    # Create a conversation object to maintain context across retries
    conversation = model.conversation()

    for attempt in range(max_attempts):
        try:
            if attempt == 0:
                # First attempt: fresh prompt with system context
                response = conversation.prompt(prompt, system=system, tools=tools)
            else:
                # Retry: Continue the conversation with a reminder
                print(
                    f"Retry {attempt}: Adding reminder to submit review...",
                    file=sys.stderr,
                )
                reminder_messages = [
                    "Please submit your review using the submit_review() tool. "
                    "Call submit_review() with your summary, issues list, and positive_aspects list.",
                    "I notice you haven't called submit_review() yet. "
                    "Please complete your review by calling:\n"
                    "submit_review(summary='...', issues=[...], positive_aspects=[...])",
                    "To complete the review, you MUST call the submit_review() tool. "
                    "Example: submit_review(summary='The changes...', issues=[], positive_aspects=['Good error handling'])\n"
                    "Please submit your review now.",
                ]
                reminder = reminder_messages[
                    min(attempt - 1, len(reminder_messages) - 1)
                ]

                # Continue the conversation with the reminder
                response = conversation.prompt(reminder, tools=tools)

            # Check if review was submitted
            if toolbox.review_submitted:
                review_data = toolbox.review_data
                print("Review successfully submitted via tool.", file=sys.stderr)
                break
            else:
                print(
                    f"Attempt {attempt + 1}: Model did not submit review.",
                    file=sys.stderr,
                )

        except Exception as e:
            print(f"Error on attempt {attempt + 1}: {e}", file=sys.stderr)

    # Fallback: Use schema without tools if no review submitted
    if not review_data:
        print("\nFalling back to schema-based review (no tools)...", file=sys.stderr)
        try:
            response = model.prompt(prompt, system=system, schema=schema)
            response_text = (
                response.text() if hasattr(response, "text") else str(response)
            )

            if response_text:
                review_data = json.loads(response_text)
            else:
                print("Error: Empty response from LLM", file=sys.stderr)
                if output_json:
                    sys.stdout = original_stdout
                return 1, {}

        except json.JSONDecodeError as e:
            print(f"Error: Failed to parse LLM response as JSON: {e}", file=sys.stderr)
            print(f"Response was: {response_text[:500]}", file=sys.stderr)
            if output_json:
                sys.stdout = original_stdout
            return 1, {}
        except Exception as e:
            print(f"Error calling LLM: {e}", file=sys.stderr)
            if output_json:
                sys.stdout = original_stdout
            return 1, {}

    # Determine exit code
    exit_code, highest_severity = determine_exit_code(review_data.get("issues", []))

    # Add metadata
    review_data["exit_code"] = exit_code
    review_data["highest_severity"] = highest_severity

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

    args = parser.parse_args()

    mode = "commit" if args.commit else "staged"
    output_json = args.json

    try:
        exit_code, _ = review_changes(mode, output_json)
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nReview cancelled by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
