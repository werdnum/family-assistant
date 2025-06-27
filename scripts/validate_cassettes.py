#!/usr/bin/env python3
"""Validate VCR cassettes for LLM integration tests."""

import re
import sys
from pathlib import Path

import yaml


def load_cassette(file_path: Path) -> dict:
    """Load a VCR cassette file."""
    with open(file_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_sensitive_data(data: str) -> list[str]:
    """Check for potential sensitive data in a string."""
    issues = []

    # Common API key patterns
    patterns = {
        "OpenAI API Key": r"sk-[a-zA-Z0-9]{48}",
        "Generic API Key": r'[aA]pi[_-]?[kK]ey["\s:=]+[a-zA-Z0-9]{20,}',
        "Bearer Token": r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*",
        "Basic Auth": r"Basic\s+[a-zA-Z0-9+/]+=*",
    }

    for name, pattern in patterns.items():
        if re.search(pattern, data):
            issues.append(f"Potential {name} found")

    return issues


def validate_cassette(file_path: Path) -> list[str]:
    """Validate a single cassette file."""
    issues = []

    try:
        cassette = load_cassette(file_path)
    except Exception as e:
        return [f"Failed to load cassette: {e}"]

    if not cassette:
        return ["Empty cassette file"]

    # Check interactions
    interactions = cassette.get("interactions", [])
    if not interactions:
        issues.append("No interactions recorded")

    for i, interaction in enumerate(interactions):
        # Check request
        request = interaction.get("request", {})

        # Check for sensitive data in headers
        headers = request.get("headers", {})
        for header_name, header_values in headers.items():
            header_lower = header_name.lower()
            if header_lower in ["authorization", "x-api-key", "api-key"]:
                for value in header_values:
                    if value != "REDACTED":
                        issues.append(
                            f"Interaction {i}: Unredacted {header_name} header"
                        )

        # Check for sensitive data in URI
        uri = request.get("uri", "")
        uri_issues = check_sensitive_data(uri)
        for issue in uri_issues:
            issues.append(f"Interaction {i}: {issue} in URI")

        # Check for sensitive data in body
        body = request.get("body", {})
        if isinstance(body, str):
            body_issues = check_sensitive_data(body)
            for issue in body_issues:
                issues.append(f"Interaction {i}: {issue} in request body")

        # Check response
        response = interaction.get("response", {})
        status = response.get("status", {})

        # Check for error responses that shouldn't be in cassettes
        status_code = status.get("code", 0)
        if status_code >= 400:
            issues.append(
                f"Interaction {i}: Error response recorded (HTTP {status_code})"
            )

    return issues


def main() -> int:
    """Main function to validate all cassettes."""
    cassette_dir = Path("tests/cassettes")

    if not cassette_dir.exists():
        print(f"Cassette directory '{cassette_dir}' not found")
        return 0

    yaml_files = list(cassette_dir.rglob("*.yaml"))
    yml_files = list(cassette_dir.rglob("*.yml"))
    all_files = yaml_files + yml_files

    if not all_files:
        print(f"No cassette files found in '{cassette_dir}'")
        return 0

    print(f"Validating {len(all_files)} cassette files...")

    total_issues = 0
    files_with_issues = 0

    for file_path in sorted(all_files):
        relative_path = file_path.relative_to(cassette_dir)
        issues = validate_cassette(file_path)

        if issues:
            files_with_issues += 1
            total_issues += len(issues)
            print(f"\n❌ {relative_path}")
            for issue in issues:
                print(f"   - {issue}")
        else:
            print(f"✅ {relative_path}")

    print(f"\n{'=' * 60}")
    print(f"Summary: {len(all_files)} files checked")
    print(f"Files with issues: {files_with_issues}")
    print(f"Total issues found: {total_issues}")

    if total_issues > 0:
        print("\n⚠️  Please fix the issues above before committing cassettes")
        return 1
    else:
        print("\n✨ All cassettes are valid!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
