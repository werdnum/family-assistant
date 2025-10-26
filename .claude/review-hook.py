#!/usr/bin/env python3
"""
Pre-commit hook for code review using the review-changes.sh script.
Handles formatting/linting, running the review, and processing the results.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class ReviewHook:
    """Handles the review hook workflow."""

    def __init__(self) -> None:
        self.repo_root = self._get_repo_root()
        self.stash_ref: str | None = None
        self.has_stashed = False

    def _get_repo_root(self) -> Path:
        """Get the repository root directory."""
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Not in a git repo, allow the command
            sys.exit(0)
        return Path(result.stdout.strip())

    def _parse_input(self) -> dict[str, Any]:
        """Parse the JSON input from stdin."""
        try:
            return json.loads(sys.stdin.read())
        except json.JSONDecodeError:
            # Invalid input, allow the command
            sys.exit(0)

    def _extract_command(self, json_input: dict[str, Any]) -> str:
        """Extract the bash command from the JSON input."""
        return json_input.get("tool_input", {}).get("command", "")

    def _is_commit_or_pr(self, command: str) -> bool:
        """Check if the command is a git commit or PR creation."""
        patterns = [r"git\s+(commit|ci)(\s|$)", r"gh\s+pr\s+create"]
        return any(re.search(pattern, command) for pattern in patterns)

    def _execute_git_adds(self, command: str) -> bool:
        """Parse and execute any git add commands in the command."""
        add_pattern = r"git\s+add\s+[^;&|]*"
        add_commands = re.findall(add_pattern, command)

        for add_cmd in add_commands:
            result = subprocess.run(
                add_cmd,
                shell=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                print(f"❌ {add_cmd} failed", file=sys.stderr)
                return False

        if add_commands:
            print("✅ Git add commands completed", file=sys.stderr)
        return True

    def _stash_unstaged_changes(self) -> bool:
        """Stash unstaged changes to isolate staged changes."""
        # Check if there are unstaged changes
        result = subprocess.run(
            ["git", "diff", "--quiet"], capture_output=True, check=False
        )
        if result.returncode == 0:
            # No unstaged changes
            return False

        # Use git stash push --keep-index to:
        # 1. Stash unstaged changes
        # 2. Keep staged changes in the index AND working directory
        # 3. Reset non-staged files to match HEAD (fixes issue where committed
        #    files that aren't re-staged would stay at their old state)
        result = subprocess.run(
            [
                "git",
                "stash",
                "push",
                "--keep-index",
                "-m",
                "review-hook: unstaged changes",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode == 0:
            # Get the stash ref for later restoration
            stash_list_result = subprocess.run(
                ["git", "stash", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            # The stash we just created is stash@{0}
            if stash_list_result.stdout:
                self.stash_ref = "stash@{0}"
                self.has_stashed = True
                print("✅ Unstaged changes stashed", file=sys.stderr)
                return True
        return False

    def _restore_stash(self) -> None:
        """Restore stashed changes."""
        if not self.has_stashed or not self.stash_ref:
            return

        print("\nRestoring stashed changes...", file=sys.stderr)
        result = subprocess.run(
            ["git", "stash", "pop", "--quiet", "--index", self.stash_ref],
            capture_output=True,
            check=False,
        )

        if result.returncode == 0:
            print("✅ Stashed changes restored successfully", file=sys.stderr)
        else:
            print("⚠️ Failed to restore stashed changes cleanly", file=sys.stderr)
            print(
                f"Your changes are preserved in {self.stash_ref}. "
                f"You can restore them with: git stash apply {self.stash_ref}",
                file=sys.stderr,
            )

    def _run_formatters_and_linters(self) -> bool:
        """Run format-and-lint.sh on staged files."""
        script_path = self.repo_root / "scripts" / "format-and-lint.sh"
        if not script_path.exists():
            print("⚠️ scripts/format-and-lint.sh not found", file=sys.stderr)
            return True

        # Get staged files
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=False,
        )
        staged_files = [f for f in result.stdout.splitlines() if f]

        if not staged_files:
            return True

        # Set up environment to include venv bin directory in PATH
        # This ensures tools like ast-grep can be found
        env = os.environ.copy()
        venv_bin = self.repo_root / ".venv" / "bin"
        if venv_bin.exists():
            env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"

        print("Running format-and-lint.sh on staged files...", file=sys.stderr)
        result = subprocess.run(
            [str(script_path)] + staged_files,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        if result.returncode != 0:
            print("❌ Formatting/linting failed", file=sys.stderr)
            if result.stdout:
                print(result.stdout, file=sys.stderr)
            if result.stderr:
                print("STDERR:", file=sys.stderr)
                print(result.stderr, file=sys.stderr)
            if not result.stdout and not result.stderr:
                print(
                    f"No output from format-and-lint.sh (exit code {result.returncode})",
                    file=sys.stderr,
                )
            return False

        # Stage any changes made by formatters
        subprocess.run(["git", "add"] + staged_files, check=False)
        print("✅ Formatting and linting completed", file=sys.stderr)
        return True

    def _run_precommit_hooks(self) -> tuple[bool, str]:
        """Run pre-commit hooks on staged files."""
        if (
            subprocess.run(
                ["which", "pre-commit"], capture_output=True, check=False
            ).returncode
            != 0
        ):
            return True, ""

        config_path = self.repo_root / ".pre-commit-config.yaml"
        if not config_path.exists():
            return True, ""

        # Set up environment to include venv bin directory in PATH
        env = os.environ.copy()
        venv_bin = self.repo_root / ".venv" / "bin"
        if venv_bin.exists():
            env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"

        print("Running pre-commit hooks...", file=sys.stderr)

        for iteration in range(5):
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                capture_output=True,
                text=True,
                check=False,
            )
            staged_files = [f for f in result.stdout.splitlines() if f]

            if not staged_files:
                break

            result = subprocess.run(
                ["pre-commit", "run", "--files"] + staged_files,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            if result.returncode == 0:
                # Check if any changes were made
                if (
                    subprocess.run(["git", "diff", "--quiet"], check=False).returncode
                    == 0
                ):
                    print("✅ Pre-commit hooks completed", file=sys.stderr)
                    break
                else:
                    print(
                        f"Pre-commit hooks made changes (iteration {iteration + 1})...",
                        file=sys.stderr,
                    )
                    subprocess.run(["git", "add", "-u"], check=False)
            else:
                print("❌ Pre-commit hooks failed", file=sys.stderr)
                error_msg = ""
                if result.stdout:
                    print("STDOUT:", file=sys.stderr)
                    print(result.stdout, file=sys.stderr)
                    error_msg += f"STDOUT:\n{result.stdout}\n"
                if result.stderr:
                    print("STDERR:", file=sys.stderr)
                    print(result.stderr, file=sys.stderr)
                    error_msg += f"STDERR:\n{result.stderr}\n"
                return False, error_msg

        return True, ""

    def _run_review(self, command: str) -> tuple[int, dict[str, Any], str]:
        """Run the review script and get JSON output.

        Args:
            command: The git command being executed (for context)

        Returns:
            Tuple of (exit_code, review_data, cache_key)
        """
        review_script = self.repo_root / "scripts" / "review-changes.py"
        if not review_script.exists():
            print("Review script not found, skipping code review", file=sys.stderr)
            return 0, {}, ""

        # Set up environment to include venv bin directory in PATH
        env = os.environ.copy()
        venv_bin = self.repo_root / ".venv" / "bin"
        if venv_bin.exists():
            env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"

        # Use venv python explicitly to ensure modules like llm are available
        venv_python = venv_bin / "python3"
        python_executable = str(venv_python) if venv_python.exists() else sys.executable

        print("\nAnalyzing staged changes for issues...", file=sys.stderr)
        cmd = [python_executable, str(review_script), "--json"]

        if command:
            cmd.extend(["--command", command])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        # The human-readable output goes to stderr, JSON to stdout
        print(result.stderr, file=sys.stderr)

        try:
            review_data = json.loads(result.stdout) if result.stdout else {}
        except json.JSONDecodeError:
            review_data = {}

        # Extract cache_key from review data
        cache_key = review_data.get("cache_key", "")

        return result.returncode, review_data, cache_key

    def _format_issues(self, issues: list[dict[str, Any]]) -> str:
        """Format issues from JSON for display."""
        if not issues:
            return "No specific issues available"

        formatted = []
        for issue in issues[:20]:  # Limit to 20 issues
            severity = issue.get("severity", "UNKNOWN")
            file = issue.get("file", "unknown")
            line = issue.get("line", "")
            desc = issue.get("description", "")

            if line:
                formatted.append(f"[{severity}] {file}:{line}: {desc}")
            else:
                formatted.append(f"[{severity}] {file}: {desc}")

        return "\n".join(formatted)

    def _check_for_sentinel(
        self, command: str, cache_key: str
    ) -> tuple[bool, bool, str, str]:
        """Check for review acknowledgment or bypass in the command.

        Args:
            command: The git command being executed
            cache_key: The review cache key for this diff

        Returns: (has_reviewed, has_bypass, bypass_reason, cache_key_prefix)
        """
        # Use first 12 characters of cache key for readability
        cache_key_prefix = cache_key[:12] if cache_key else "no-cache"
        sentinel = f"Reviewed: cache-{cache_key_prefix}"

        has_reviewed = sentinel in command

        # Check for bypass
        bypass_match = re.search(r"Bypass-Review:\s*([^\"'\n]+)", command)
        has_bypass = False
        bypass_reason = ""

        if bypass_match:
            bypass_reason = bypass_match.group(1).strip()
            if bypass_reason and bypass_reason not in {"<reason>", "reason"}:
                has_bypass = True

        return has_reviewed, has_bypass, bypass_reason, cache_key_prefix

    def _output_json_response(self, permission: str, reason: str) -> None:
        """Output the JSON response for the hook."""
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": permission,
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(response))

    def run(self) -> None:
        """Main workflow execution."""
        # Skip review when running in remote Claude Code session without API key
        # (resource-constrained environment needs external LLM API)
        if os.getenv("CLAUDE_CODE_REMOTE", "false").lower() == "true" and not os.getenv(
            "OPENROUTER_KEY"
        ):
            sys.exit(0)

        try:
            # Parse input
            json_input = self._parse_input()
            command = self._extract_command(json_input)

            # Check if this is a commit/PR command
            if not self._is_commit_or_pr(command):
                sys.exit(0)

            print(
                "🔍 Running improved pre-commit review workflow...\n", file=sys.stderr
            )

            # Step 1: Execute git add commands
            if not self._execute_git_adds(command):
                sys.exit(0)

            # Step 2: Stash unstaged changes
            self._stash_unstaged_changes()

            # Step 3: Run formatters and linters
            if not self._run_formatters_and_linters():
                print(
                    "Formatting/linting failed. Please fix the issues before committing.",
                    file=sys.stderr,
                )
                sys.exit(2)

            # Step 4: Run pre-commit hooks
            success, error_msg = self._run_precommit_hooks()
            if not success:
                print(
                    "Pre-commit hooks failed. Please fix the issues before committing.",
                    file=sys.stderr,
                )
                sys.exit(2)

            # Step 5: Run code review
            exit_code, review_data, cache_key = self._run_review(command)

            # Check for sentinel phrases
            has_reviewed, has_bypass, bypass_reason, cache_key_prefix = (
                self._check_for_sentinel(command, cache_key)
            )

            # Process based on exit code and sentinels
            if exit_code == 0:
                sys.exit(0)
            elif exit_code == 1:
                # Minor issues
                if has_reviewed:
                    sys.exit(0)
                elif has_bypass:
                    self._output_json_response(
                        "ask",
                        f"Minor issues found. Bypass requested: {bypass_reason}\n\nDo you want to proceed?",
                    )
                else:
                    issues = review_data.get("issues", [])
                    formatted_issues = self._format_issues(issues)
                    print(
                        f"Code review found minor issues:\n\n{formatted_issues}\n\n"
                        f"These issues should be fixed before committing.\n"
                        f"If you have a specific reason not to fix them, you may acknowledge them by adding:\n"
                        f"• Reviewed: cache-{cache_key_prefix}\n\n"
                        f"However, fixing the issues is strongly preferred over acknowledgment.",
                        file=sys.stderr,
                    )
                    sys.exit(2)
            elif has_bypass:
                # Major issues (exit code 2) with bypass request - escalate to user
                self._output_json_response(
                    "ask",
                    f"BLOCKING issues found. Escalation requested: {bypass_reason}\n\n"
                    "These are serious issues (potential build breaks, runtime errors, security risks, or logic errors) "
                    "that should typically be fixed.\n\n"
                    "You may proceed if the review is incorrect or contradicts the user's explicit instructions. "
                    "Do you want to proceed?",
                )
            elif has_reviewed:
                # Blocking issues with has_reviewed - can't bypass with Reviewed
                issues = review_data.get("issues", [])
                formatted_issues = self._format_issues(issues)
                print(
                    f"BLOCKING issues found that cannot be bypassed with 'Reviewed' acknowledgment:\n\n"
                    f"{formatted_issues}\n\n"
                    "These serious issues must be fixed before committing.\n"
                    "'Reviewed' acknowledgment is only for minor issues.\n\n"
                    "If you believe the review is incorrect or contradicts the user's explicit instructions, "
                    "escalate for manual decision: Bypass-Review: <why the review is incorrect>",
                    file=sys.stderr,
                )
                sys.exit(2)
            else:
                # Blocking issues without bypass - exit with error message
                issues = review_data.get("issues", [])
                formatted_issues = self._format_issues(issues)
                print(
                    f"Code review found BLOCKING issues:\n\n{formatted_issues}\n\n"
                    "These serious issues must be fixed before committing.\n\n"
                    "If you believe the review is incorrect or contradicts the user's explicit instructions, "
                    "you may escalate for manual decision: Bypass-Review: <why the review is incorrect>",
                    file=sys.stderr,
                )
                sys.exit(2)

        finally:
            # Always restore stash on exit
            self._restore_stash()


if __name__ == "__main__":
    hook = ReviewHook()
    hook.run()
