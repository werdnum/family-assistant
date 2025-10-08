#!/usr/bin/env python3
"""
Fast linting hook for Claude PostToolUse.
Runs appropriate linters after file edits with timing and actionable feedback.
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LintResult:
    """Result from running a linter."""

    name: str
    success: bool
    duration: float
    output: str = ""
    error: str = ""
    auto_fixable: bool = False


async def run_command(
    cmd: list[str], timeout: float = 5.0
) -> tuple[int, str, str, float]:
    """Run a command asynchronously with timeout and return (returncode, stdout, stderr, duration)."""
    start_time = time.time()
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            duration = time.time() - start_time
            return process.returncode or 0, stdout, stderr, duration
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            duration = time.time() - start_time
            return -1, "", f"Timeout after {timeout}s", duration
    except Exception as e:
        duration = time.time() - start_time
        return -1, "", str(e), duration


async def run_hints(
    file_path: str, tool_name: str, tool_input: dict[str, Any]
) -> LintResult:
    """Run hints checker and filter to only new/changed code."""
    cmd = [".ast-grep/check-hints.py", "--json", file_path]
    returncode, stdout, stderr, duration = await run_command(cmd, timeout=2.0)

    if returncode != 0 or not stdout:
        # No hints or error - always succeed
        return LintResult("hints", True, duration)

    try:
        hints = json.loads(stdout) if stdout else []
        if not hints:
            return LintResult("hints", True, duration)

        # Filter hints based on tool type
        filtered_hints = []

        if tool_name == "Edit":
            # For Edit: only show hints where matched code appears in new_string
            new_string = tool_input.get("new_string", "")
            for hint in hints:
                matched_text = hint.get("text", "")
                if matched_text and matched_text in new_string:
                    filtered_hints.append(hint)

        elif tool_name == "Write":
            # For Write: only show hints if file is not tracked by git (new file)
            check_cmd = ["git", "ls-files", "--error-unmatch", file_path]
            returncode, _, _, _ = await run_command(check_cmd, timeout=1.0)
            if returncode != 0:
                # File not tracked - show all hints
                filtered_hints = hints

        if filtered_hints:
            output = f"Code hints for {file_path}:\n"
            for hint in filtered_hints:
                line = hint.get("range", {}).get("start", {}).get("line", "?")
                rule_id = hint.get("ruleId", "unknown")
                message = hint.get("message", "")
                output += f"  Line {line}: 💡 [{rule_id}] {message}\n"

            return LintResult(
                "hints",
                success=True,  # Hints never fail
                duration=duration,
                output=output.strip(),
            )

        return LintResult("hints", True, duration)

    except json.JSONDecodeError:
        return LintResult("hints", True, duration)


async def lint_python_file(
    file_path: str, tool_name: str = "", tool_input: dict[str, Any] | None = None
) -> list[LintResult]:
    """Run fast Python linters on a file in parallel."""
    venv = os.environ.get("VIRTUAL_ENV", ".venv")
    tool_input = tool_input or {}

    # Define all linter tasks
    async def run_code_conformance() -> LintResult:
        """Run code conformance check (ast-grep)."""
        cmd = [".ast-grep/check-conformance.py", "--json", file_path]
        returncode, stdout, stderr, duration = await run_command(cmd, timeout=2.0)

        if returncode != 0 and stdout:
            # Parse JSON to extract violations
            try:
                violations = json.loads(stdout) if stdout else []
                if violations:
                    output = f"Code conformance violations in {file_path}:\n"
                    for v in violations:
                        line = v.get("range", {}).get("start", {}).get("line", "?")
                        rule_id = v.get("ruleId", "unknown")
                        message = v.get("message", "")
                        output += f"  Line {line}: [{rule_id}] {message}\n"

                    return LintResult(
                        "code-conformance",
                        success=False,
                        duration=duration,
                        output=output.strip(),
                        auto_fixable=False,
                    )
            except json.JSONDecodeError:
                pass

        return LintResult("code-conformance", True, duration)

    async def run_ruff_check() -> LintResult:
        # Run Ruff without auto-fixing to avoid mutating files while work is in progress.
        check_cmd = [
            f"{venv}/bin/ruff",
            "check",
            "--preview",
            "--ignore=E501",
            file_path,
        ]
        returncode, stdout, stderr, duration = await run_command(check_cmd, timeout=2.0)

        if returncode != 0:
            # Capture the output from the basic check run first.
            output = stderr or stdout

            # Run Ruff again with --diff so we can show the suggested fixes without
            # mutating the file. This mirrors what `ruff check --fix` would do, but
            # surfaces the diff instead of applying it.
            diff_cmd = [
                f"{venv}/bin/ruff",
                "check",
                "--diff",
                "--unsafe-fixes",
                "--preview",
                "--ignore=E501",
                file_path,
            ]
            _, diff_stdout, diff_stderr, _ = await run_command(diff_cmd, timeout=2.0)

            diff_output = diff_stdout or diff_stderr
            if diff_output:
                output += (
                    "\n💡 Suggested fixes (run `ruff check --fix --preview --ignore=E501"
                    f" {file_path}` to apply):\n{diff_output}"
                )

            return LintResult(
                "ruff check",
                success=False,
                duration=duration,
                output=output,
                auto_fixable=bool(diff_output.strip()),
            )
        else:
            return LintResult("ruff check", True, duration)

    async def run_ruff_format() -> LintResult:
        cmd = [f"{venv}/bin/ruff", "format", file_path]
        returncode, stdout, stderr, duration = await run_command(cmd, timeout=5.0)

        if returncode != 0:
            output = stderr or stdout or "ruff format failed"
            return LintResult(
                "ruff format",
                success=False,
                duration=duration,
                output=output,
            )
        else:
            return LintResult("ruff format", True, duration)

    async def run_basedpyright() -> LintResult:
        cmd = [f"{venv}/bin/basedpyright", file_path]
        returncode, stdout, stderr, duration = await run_command(cmd, timeout=8.0)

        if returncode != 0:
            output = stdout or stderr
            # Extract just the error messages, not the full output
            lines = output.split("\n")
            errors = [line for line in lines if "error:" in line.lower()]
            if errors:
                output = "\n".join(errors[:5])  # Limit to first 5 errors
                if len(errors) > 5:
                    output += f"\n... and {len(errors) - 5} more errors"

            return LintResult(
                "basedpyright", success=False, duration=duration, output=output
            )
        else:
            return LintResult("basedpyright", True, duration)

    # Run all linters in parallel
    format_result = await run_ruff_format()
    other_tasks = [
        run_ruff_check(),
        run_basedpyright(),
        run_code_conformance(),
        run_hints(file_path, tool_name, tool_input),
    ]

    other_results = await asyncio.gather(*other_tasks)
    return [format_result, *other_results]


async def lint_javascript_file(file_path: str) -> list[LintResult]:
    """Run fast JavaScript/TypeScript linters on a file in parallel."""
    # Check if we're in a frontend directory
    file_path_obj = Path(file_path)
    frontend_dir = Path.cwd() / "frontend"

    if not file_path_obj.is_relative_to(frontend_dir):
        return []

    # Convert to relative path from frontend directory for npm commands
    relative_path = str(file_path_obj.relative_to(frontend_dir))

    async def run_biome_format() -> LintResult:
        cmd = ["npm", "run", "format", "--prefix", "frontend", "--", relative_path]
        returncode, stdout, stderr, duration = await run_command(cmd, timeout=5.0)

        if returncode != 0:
            return LintResult(
                "biome format",
                success=False,
                duration=duration,
                output=stderr or stdout,
                auto_fixable=True,
            )
        else:
            return LintResult("biome format", True, duration)

    async def run_eslint() -> LintResult:
        cmd = ["npm", "run", "lint:fix", "--prefix", "frontend", "--", relative_path]
        returncode, stdout, stderr, duration = await run_command(cmd, timeout=8.0)

        if returncode != 0:
            return LintResult(
                "eslint",
                success=False,
                duration=duration,
                output=stderr or stdout,
                auto_fixable=True,
            )
        else:
            return LintResult("eslint", True, duration)

    # Run both linters in parallel
    tasks = [
        run_biome_format(),
        run_eslint(),
    ]

    results = await asyncio.gather(*tasks)
    return list(results)


def format_results(file_path: str, results: list[LintResult]) -> dict[str, Any]:
    """Format lint results for output."""
    total_duration = sum(r.duration for r in results)
    has_errors = any(not r.success for r in results)
    has_auto_fixable = any(r.auto_fixable for r in results)

    output_lines = []

    if has_errors:
        output_lines.append(f"🔍 Lint issues in {file_path} ({total_duration:.2f}s)")
        output_lines.append("")

        for result in results:
            if not result.success:
                output_lines.append(f"❌ {result.name} ({result.duration:.2f}s)")
                if result.output:
                    # Indent the output
                    for line in result.output.split("\n"):
                        if line.strip():
                            output_lines.append(f"   {line}")
                output_lines.append("")

        if has_auto_fixable:
            output_lines.append("💡 Some issues can be auto-fixed")

        output_lines.append("")
        output_lines.append("ℹ️  Note: It's okay to temporarily ignore these if you're")
        output_lines.append(
            "   actively working on related changes that will fix them."
        )
    else:
        # All checks passed - brief success message
        linter_names = [r.name for r in results]
        output_lines.append(
            f"✅ {file_path}: {', '.join(linter_names)} ({total_duration:.2f}s)"
        )

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(output_lines) if output_lines else None,
        }
    }


async def main() -> None:
    """Main entry point for the hook."""
    try:
        # Read tool data from stdin
        tool_data = json.loads(sys.stdin.read())

        tool_name = tool_data.get("tool_name", "")
        tool_input = tool_data.get("tool_input", {})

        # Only process file editing tools
        # NotebookEdit is excluded since it only works with .ipynb files,
        # which require special cell-based linting not yet implemented
        if tool_name not in {"Edit", "MultiEdit", "Write"}:
            return

        # Extract file path from tool input
        file_path = tool_input.get("file_path")
        if not file_path:
            return

        file_paths = [file_path]

        # Define async function to process a single file
        async def process_file(file_path: str) -> tuple[str, list[LintResult]] | None:
            if not os.path.exists(file_path):
                return None

            # Determine file type and run appropriate linters
            file_ext = Path(file_path).suffix.lower()

            if file_ext == ".py":
                results = await lint_python_file(file_path, tool_name, tool_input)
            elif file_ext in {".js", ".jsx", ".ts", ".tsx"}:
                results = await lint_javascript_file(file_path)
            else:
                # Skip unsupported file types
                # Note: .ipynb notebooks would need special cell-based linting
                return None

            return (file_path, results) if results else None

        # Process all files in parallel (though typically there's only one)
        tasks = [process_file(fp) for fp in file_paths]
        file_results = await asyncio.gather(*tasks)

        # Filter out None results and build the results dict
        all_results = {}
        for result in file_results:
            if result is not None:
                fp, res = result
                all_results[fp] = res

        # Format and output results
        if all_results:
            combined_output = []
            for file_path, results in all_results.items():
                formatted = format_results(file_path, results)
                if formatted["hookSpecificOutput"]["additionalContext"]:
                    combined_output.append(
                        formatted["hookSpecificOutput"]["additionalContext"]
                    )

            if combined_output:
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": "\n".join(combined_output),
                    }
                }
                print(json.dumps(output))

    except Exception as e:
        # Log error but don't block the tool
        error_output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"Lint hook error: {str(e)}",
            }
        }
        print(json.dumps(error_output))


if __name__ == "__main__":
    asyncio.run(main())
