#!/usr/bin/env python3
"""
Code conformance checker with exemption support.

This script wraps ast-grep to provide exemption capabilities:
- Inline exemptions: # ast-grep-ignore: <rule-id> - <reason>
- Block exemptions: # ast-grep-ignore-block: <rule-id> - <reason> ... # ast-grep-ignore-end
- File-level exemptions: Defined in .ast-grep/exemptions.yml

Usage:
    .ast-grep/check-conformance.py [files...]
    .ast-grep/check-conformance.py --json [files...]
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "Warning: PyYAML not installed. File-level exemptions from .ast-grep/exemptions.yml will be ignored.",
        file=sys.stderr,
    )
    yaml = None  # type: ignore[assignment]


@dataclass
class Exemption:
    """Represents a code conformance exemption."""

    rule_id: str
    file_path: str
    line_number: int | None
    reason: str
    exemption_type: str  # "inline", "block", "file"


@dataclass
class Violation:
    """Represents a code conformance violation."""

    rule_id: str
    file_path: str
    line_number: int
    message: str
    raw_data: dict[str, Any]


def parse_inline_exemptions(file_path: str) -> list[Exemption]:
    """Parse inline and block exemptions from a source file."""
    exemptions = []
    try:
        with open(file_path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return exemptions

    block_exemption: Exemption | None = None

    for i, line in enumerate(lines, start=1):
        # Check for block start
        match = re.match(
            r"^\s*#\s*ast-grep-ignore-block:\s*(\S+)\s*-\s*(.+)$", line.strip()
        )
        if match:
            rule_id, reason = match.groups()
            block_exemption = Exemption(
                rule_id=rule_id.strip(),
                file_path=file_path,
                line_number=i,
                reason=reason.strip(),
                exemption_type="block",
            )
            continue

        # Check for block end
        if re.match(r"^\s*#\s*ast-grep-ignore-end\s*$", line.strip()):
            block_exemption = None
            continue

        # Check for inline exemption
        match = re.match(r"^\s*#\s*ast-grep-ignore:\s*(\S+)\s*-\s*(.+)$", line.strip())
        if match:
            rule_id, reason = match.groups()
            # Inline exemption applies to the next line
            exemptions.append(
                Exemption(
                    rule_id=rule_id.strip(),
                    file_path=file_path,
                    line_number=i + 1,
                    reason=reason.strip(),
                    exemption_type="inline",
                )
            )
            continue

        # If we're in a block exemption, add an exemption for this line
        if block_exemption and not line.strip().startswith("#"):
            exemptions.append(
                Exemption(
                    rule_id=block_exemption.rule_id,
                    file_path=file_path,
                    line_number=i,
                    reason=block_exemption.reason,
                    exemption_type="block",
                )
            )

    return exemptions


def load_file_exemptions(
    exemptions_file: str = ".ast-grep/exemptions.yml",
) -> list[Exemption]:
    """Load file-level exemptions from YAML configuration."""
    if yaml is None:
        return []

    exemptions = []
    exemptions_path = Path(exemptions_file)

    if not exemptions_path.exists():
        return exemptions

    try:
        with open(exemptions_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "exemptions" not in data:
            return exemptions

        for entry in data["exemptions"]:
            rule_id = entry.get("rule", "")
            files = entry.get("files", [])
            reason = entry.get("reason", "No reason provided")

            for file_pattern in files:
                # Convert glob patterns to exemptions
                exemptions.append(
                    Exemption(
                        rule_id=rule_id,
                        file_path=file_pattern,
                        line_number=None,
                        reason=reason,
                        exemption_type="file",
                    )
                )

    except Exception as e:
        print(f"Warning: Failed to load exemptions file: {e}", file=sys.stderr)

    return exemptions


def run_ast_grep(files: list[str]) -> list[Violation]:
    """Run ast-grep and return violations."""
    cmd = ["ast-grep", "scan", "--json"]

    if files:
        cmd.extend(files)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if not result.stdout:
            return []

        # Parse JSON output
        data = json.loads(result.stdout)

        # Rules that only apply to test files
        test_only_rules = {
            "no-asyncio-sleep-in-tests",
            "no-time-sleep-in-tests",
            "no-playwright-wait-for-timeout",
        }

        violations = []
        for item in data:
            file_path = item.get("file", "")
            rule_id = item.get("ruleId", "unknown")

            # Skip test-only rules for non-test files
            if rule_id in test_only_rules and not file_path.startswith("tests/"):
                continue

            violations.append(
                Violation(
                    rule_id=rule_id,
                    file_path=file_path,
                    line_number=item.get("range", {}).get("start", {}).get("line", 0),
                    message=item.get("message", ""),
                    raw_data=item,
                )
            )

        return violations

    except subprocess.CalledProcessError as e:
        print(f"Error running ast-grep: {e}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing ast-grep output: {e}", file=sys.stderr)
        return []


def is_violation_exempted(
    violation: Violation, exemptions: list[Exemption]
) -> tuple[bool, Exemption | None]:
    """Check if a violation is exempted."""
    for exemption in exemptions:
        # Check rule ID matches
        if exemption.rule_id != violation.rule_id:
            continue

        # File-level exemption (glob pattern match)
        if exemption.exemption_type == "file":
            if Path(violation.file_path).match(exemption.file_path):
                return True, exemption

        # Line-specific exemption (inline or block)
        elif (
            exemption.file_path == violation.file_path
            and exemption.line_number == violation.line_number
        ):
            return True, exemption

    return False, None


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Check code conformance with exemption support"
    )
    parser.add_argument("files", nargs="*", help="Files to check (default: all)")
    parser.add_argument(
        "--json", action="store_true", help="Output results in JSON format"
    )
    parser.add_argument(
        "--exemptions-file",
        default=".ast-grep/exemptions.yml",
        help="Path to exemptions file",
    )

    args = parser.parse_args()

    # Load file-level exemptions
    file_exemptions = load_file_exemptions(args.exemptions_file)

    # Run ast-grep
    violations = run_ast_grep(args.files)

    # Load inline exemptions for each file with violations
    inline_exemptions: list[Exemption] = []
    checked_files = set()
    for violation in violations:
        if violation.file_path not in checked_files:
            inline_exemptions.extend(parse_inline_exemptions(violation.file_path))
            checked_files.add(violation.file_path)

    all_exemptions = file_exemptions + inline_exemptions

    # Filter out exempted violations
    remaining_violations = []
    exempted_violations = []

    for violation in violations:
        is_exempted, exemption = is_violation_exempted(violation, all_exemptions)
        if is_exempted:
            exempted_violations.append((violation, exemption))
        else:
            remaining_violations.append(violation)

    # Output results
    if args.json:
        output = [v.raw_data for v in remaining_violations]
        print(json.dumps(output, indent=2))
    else:
        if remaining_violations:
            print("Code Conformance Violations:")
            print()
            for v in remaining_violations:
                print(f"{v.file_path}:{v.line_number}")
                print(f"  [{v.rule_id}] {v.message}")
                if "note" in v.raw_data:
                    print(f"  Note: {v.raw_data['note']}")
                print()

        if exempted_violations and not args.json:
            print(f"\n{len(exempted_violations)} violations exempted")

    return 1 if remaining_violations else 0


if __name__ == "__main__":
    sys.exit(main())
