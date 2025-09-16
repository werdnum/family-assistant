#!/usr/bin/env python3
"""
Pre-commit hook for code review using the review-changes.sh script.
Handles formatting/linting, running the review, and processing the results.
"""

import json
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
            result = subprocess.run(add_cmd, shell=True, capture_output=True)
            if result.returncode != 0:
                print(f"❌ {add_cmd} failed", file=sys.stderr)
                return False

        if add_commands:
            print("✅ Git add commands completed", file=sys.stderr)
        return True

    def _stash_unstaged_changes(self) -> bool:
        """Stash unstaged changes to isolate staged changes."""
        # Check if there are unstaged changes
        result = subprocess.run(["git", "diff", "--quiet"], capture_output=True)
        if result.returncode == 0:
            # No unstaged changes
            return False

        # Create a stash
        result = subprocess.run(
            ["git", "stash", "create", "review-hook: unstaged changes"],
            capture_output=True,
            text=True,
        )

        if result.stdout.strip():
            self.stash_ref = result.stdout.strip()
            # Store the stash
            subprocess.run(
                [
                    "git",
                    "stash",
                    "store",
                    "-m",
                    "review-hook: unstaged changes",
                    self.stash_ref,
                ],
                check=False,
            )
            # Reset to match index
            subprocess.run(["git", "checkout-index", "-a", "-f"], check=False)
            self.has_stashed = True
            print(
                f"✅ Unstaged changes stashed (ref: {self.stash_ref[:8]})",
                file=sys.stderr,
            )
            return True
        return False

    def _restore_stash(self) -> None:
        """Restore stashed changes."""
        if not self.has_stashed or not self.stash_ref:
            return

        print("\nRestoring stashed changes...", file=sys.stderr)
        result = subprocess.run(
            ["git", "stash", "apply", "--quiet", self.stash_ref], capture_output=True
        )

        if result.returncode == 0:
            subprocess.run(
                ["git", "stash", "drop", "--quiet", self.stash_ref], check=False
            )
            print("✅ Stashed changes restored successfully", file=sys.stderr)
        else:
            print("⚠️ Failed to restore stashed changes cleanly", file=sys.stderr)
            print(
                f"Your changes are preserved in stash: {self.stash_ref[:8]}",
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
            ["git", "diff", "--cached", "--name-only"], capture_output=True, text=True
        )
        staged_files = [f for f in result.stdout.splitlines() if f]

        if not staged_files:
            return True

        print("Running format-and-lint.sh on staged files...", file=sys.stderr)
        result = subprocess.run(
            [str(script_path)] + staged_files, capture_output=True, text=True
        )

        if result.returncode != 0:
            print("❌ Formatting/linting failed", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
            return False

        # Stage any changes made by formatters
        subprocess.run(["git", "add"] + staged_files, check=False)
        print("✅ Formatting and linting completed", file=sys.stderr)
        return True

    def _run_precommit_hooks(self) -> tuple[bool, str]:
        """Run pre-commit hooks on staged files."""
        if subprocess.run(["which", "pre-commit"], capture_output=True).returncode != 0:
            return True, ""

        config_path = self.repo_root / ".pre-commit-config.yaml"
        if not config_path.exists():
            return True, ""

        print("Running pre-commit hooks...", file=sys.stderr)

        for iteration in range(5):
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                capture_output=True,
                text=True,
            )
            staged_files = [f for f in result.stdout.splitlines() if f]

            if not staged_files:
                break

            result = subprocess.run(
                ["pre-commit", "run", "--files"] + staged_files,
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                # Check if any changes were made
                if subprocess.run(["git", "diff", "--quiet"]).returncode == 0:
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

    def _run_review(self) -> tuple[int, dict[str, Any]]:
        """Run the review script and get JSON output."""
        review_script = self.repo_root / "scripts" / "review-changes.sh"
        if not review_script.exists():
            print("Review script not found, skipping code review", file=sys.stderr)
            return 0, {}

        print("\nAnalyzing staged changes for issues...", file=sys.stderr)
        result = subprocess.run(
            [str(review_script), "--json"], capture_output=True, text=True
        )

        # The human-readable output goes to stderr, JSON to stdout
        print(result.stderr, file=sys.stderr)

        try:
            review_data = json.loads(result.stdout) if result.stdout else {}
        except json.JSONDecodeError:
            review_data = {}

        return result.returncode, review_data

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

    def _check_for_sentinel(self, command: str) -> tuple[bool, bool, str, str]:
        """Check for review acknowledgment or bypass in the command.

        Returns: (has_reviewed, has_bypass, bypass_reason, head_commit)
        """
        # Get current HEAD
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
        )
        head_commit = result.stdout.strip() if result.returncode == 0 else "no-head"
        sentinel = f"Reviewed: HEAD-{head_commit}"

        has_reviewed = sentinel in command

        # Check for bypass
        bypass_match = re.search(r"Bypass-Review:\s*([^\"'\n]+)", command)
        has_bypass = False
        bypass_reason = ""

        if bypass_match:
            bypass_reason = bypass_match.group(1).strip()
            if bypass_reason and bypass_reason not in ["<reason>", "reason"]:
                has_bypass = True

        return has_reviewed, has_bypass, bypass_reason, head_commit

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
                self._output_json_response(
                    "deny",
                    "Formatting/linting failed. Please fix the issues before committing.",
                )
                return

            # Step 4: Run pre-commit hooks
            success, error_msg = self._run_precommit_hooks()
            if not success:
                self._output_json_response(
                    "deny",
                    f"Pre-commit hooks failed. Please fix the issues before committing.\n\n{error_msg}",
                )
                return

            # Step 5: Run code review
            exit_code, review_data = self._run_review()

            # Check for sentinel phrases
            has_reviewed, has_bypass, bypass_reason, head_commit = (
                self._check_for_sentinel(command)
            )

            # Get issues and severity
            issues = review_data.get("issues", [])
            review_data.get("highest_severity", "")

            # Process based on exit code and sentinels
            if exit_code == 0:
                self._output_json_response("allow", "All checks passed")
            elif exit_code == 1:
                # Minor issues
                if has_reviewed:
                    self._output_json_response(
                        "allow",
                        f"Minor issues acknowledged with Reviewed: HEAD-{head_commit}",
                    )
                elif has_bypass:
                    self._output_json_response(
                        "ask",
                        f"Minor issues found. Bypass requested: {bypass_reason}\n\nDo you want to proceed?",
                    )
                else:
                    formatted_issues = self._format_issues(issues)
                    self._output_json_response(
                        "deny",
                        f"Code review found minor issues:\n\n{formatted_issues}\n\n"
                        f"To acknowledge and proceed, add to your commit message:\n"
                        f"• Reviewed: HEAD-{head_commit}",
                    )
            else:
                # Major issues (exit code 2)
                if has_bypass:
                    self._output_json_response(
                        "ask",
                        f"MAJOR issues found. Bypass requested: {bypass_reason}\n\n"
                        "These are serious issues that could break the build. Do you want to proceed anyway?",
                    )
                elif has_reviewed:
                    formatted_issues = self._format_issues(issues)
                    self._output_json_response(
                        "deny",
                        f"MAJOR issues found that cannot be bypassed with 'Reviewed' acknowledgment:\n\n"
                        f"{formatted_issues}\n\n"
                        "To bypass major issues, use: Bypass-Review: <reason why this is safe>",
                    )
                else:
                    formatted_issues = self._format_issues(issues)
                    self._output_json_response(
                        "deny",
                        f"Code review found BLOCKING issues:\n\n{formatted_issues}\n\n"
                        "These should be fixed before committing.\n\n"
                        "To override (use with caution): Bypass-Review: <specific reason>",
                    )

        finally:
            # Always restore stash on exit
            self._restore_stash()


if __name__ == "__main__":
    hook = ReviewHook()
    hook.run()
