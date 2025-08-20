#!/usr/bin/env python3
"""
JSON settings merger for oneshot mode configuration.

This script merges base Claude settings with oneshot-specific settings,
using the jsonmerge library to properly concatenate permission arrays
instead of replacing them.
"""

import json
import sys

try:
    from jsonmerge import merge  # type: ignore[import-untyped]
except ImportError:
    print(
        "Error: jsonmerge library not found. Please install it with: pip install jsonmerge",
        file=sys.stderr,
    )
    sys.exit(1)


def merge_settings(base_file: str, oneshot_file: str) -> dict:
    """
    Merge base settings with oneshot settings, concatenating permission arrays.

    Args:
        base_file: Path to base settings JSON file
        oneshot_file: Path to oneshot settings JSON file

    Returns:
        Merged settings dictionary
    """
    try:
        with open(base_file) as f:
            base = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading base settings file '{base_file}': {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(oneshot_file) as f:
            oneshot = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(
            f"Error reading oneshot settings file '{oneshot_file}': {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Define merge schema with append strategy for permission arrays
    merge_schema = {
        "properties": {
            "permissions": {
                "properties": {
                    "allow": {"mergeStrategy": "append"},
                    "deny": {"mergeStrategy": "append"},
                }
            }
        }
    }

    # Merge the settings
    try:
        merged = merge(base, oneshot, schema=merge_schema)
        return merged
    except Exception as e:
        print(f"Error merging settings: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Main entry point for the script."""
    if len(sys.argv) != 3:
        print(
            "Usage: merge-settings.py <base_settings.json> <oneshot_settings.json>",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print(
            "Merges two JSON settings files, concatenating permission arrays.",
            file=sys.stderr,
        )
        sys.exit(1)

    base_file = sys.argv[1]
    oneshot_file = sys.argv[2]

    # Merge the settings
    merged = merge_settings(base_file, oneshot_file)

    # Output the merged JSON
    print(json.dumps(merged, indent=2))


if __name__ == "__main__":
    main()
