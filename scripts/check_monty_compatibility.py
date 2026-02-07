"""Check persisted scripts in the database for Monty compatibility.

Queries event_listeners and schedule_automations tables, extracts all
Starlark scripts, and attempts to parse each with Monty to identify
incompatibilities.

Usage:
    python scripts/check_monty_compatibility.py
    python scripts/check_monty_compatibility.py --database-url sqlite+aiosqlite:///family_assistant.db
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import textwrap

import pydantic_monty
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Known external functions that scripts may call
KNOWN_EXTERNAL_FUNCTIONS = [
    # JSON API
    "json_encode",
    "json_decode",
    # Time API
    "time_now",
    "time_now_utc",
    "time_create",
    "time_from_timestamp",
    "time_parse",
    "time_in_location",
    "time_format",
    "time_add",
    "time_add_duration",
    "time_year",
    "time_month",
    "time_day",
    "time_hour",
    "time_minute",
    "time_second",
    "time_weekday",
    "time_before",
    "time_after",
    "time_equal",
    "time_diff",
    "duration_parse",
    "duration_human",
    "timezone_is_valid",
    "timezone_offset",
    "is_between",
    "is_weekend",
    # Script control
    "wake_llm",
    "print",
    # Tools API
    "tools_list",
    "tools_get",
    "tools_execute",
    "tools_execute_json",
    # Attachment API
    "attachment_get",
    "attachment_read",
    "attachment_create",
]

# Known inputs (constants + common event data variables)
KNOWN_INPUTS = [
    "NANOSECOND",
    "MICROSECOND",
    "MILLISECOND",
    "SECOND",
    "MINUTE",
    "HOUR",
    "DAY",
    "WEEK",
    "event",
]


def check_script_compatibility(
    script: str,
    script_id: str,
    script_type: str,
    is_condition: bool = False,
) -> dict:
    """Try to parse a script with Monty and report results."""
    result = {
        "id": script_id,
        "type": script_type,
        "script": script,
        "compatible": False,
        "error": None,
        "warnings": [],
    }

    if not script or not script.strip():
        result["compatible"] = True
        result["warnings"].append("Empty script")
        return result

    # Check for Starlark-only features
    if "fail(" in script:
        result["warnings"].append(
            "Uses fail() — not available in Monty. Use raise Exception() instead."
        )
    if "struct(" in script:
        result["warnings"].append(
            "Uses struct() — not available in Monty. Use dicts instead."
        )
    if script.strip().startswith("load("):
        result["warnings"].append("Uses load() — not available in Monty.")

    # For condition scripts, wrap the same way EventConditionEvaluator does
    if is_condition:
        if "return" not in script:
            script = f"def _evaluate():\n    return {script}\n\n_evaluate()"
        else:
            indented = textwrap.indent(script, "    ")
            script = f"def _evaluate():\n{indented}\n\n_evaluate()"

    # Collect any tool-like function names from the script
    # (tools are registered dynamically, so we can't know them all statically)
    extra_functions = []
    # Simple heuristic: look for foo(...) calls that aren't Python builtins
    calls = re.findall(r"\b([a-z_][a-z0-9_]*)\s*\(", script)
    python_builtins = {
        "len",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "tuple",
        "set",
        "range",
        "enumerate",
        "zip",
        "sorted",
        "reversed",
        "min",
        "max",
        "sum",
        "abs",
        "all",
        "any",
        "type",
        "isinstance",
        "hasattr",
        "getattr",
        "repr",
    }
    for call in calls:
        if (
            call not in KNOWN_EXTERNAL_FUNCTIONS
            and call not in python_builtins
            and call not in extra_functions
            and not call.startswith("_")
        ):
            extra_functions.append(call)

    all_external = KNOWN_EXTERNAL_FUNCTIONS + extra_functions

    try:
        pydantic_monty.Monty(
            script,
            inputs=KNOWN_INPUTS,
            external_functions=all_external,
        )
        result["compatible"] = True
    except pydantic_monty.MontySyntaxError as e:
        result["error"] = f"Syntax error: {e}"
    except pydantic_monty.MontyError as e:
        result["error"] = f"Parse error: {e}"
    except Exception as e:
        result["error"] = f"Unexpected error: {e}"

    if extra_functions:
        result["warnings"].append(
            f"Calls unrecognized functions (probably tools): {extra_functions}"
        )

    return result


async def fetch_scripts(database_url: str) -> list[dict]:
    """Fetch all persisted scripts from the database."""
    engine = create_async_engine(database_url)
    scripts = []

    async with AsyncSession(engine) as session:
        # 1. Event listener condition scripts
        try:
            rows = await session.execute(
                text(
                    "SELECT id, name, condition_script "
                    "FROM event_listeners "
                    "WHERE condition_script IS NOT NULL"
                )
            )
            for row in rows:
                scripts.append({
                    "id": f"event_listener:{row[0]}",
                    "name": row[1],
                    "script": row[2],
                    "type": "condition",
                    "is_condition": True,
                })
        except Exception as e:
            logger.warning(f"Could not query event_listeners: {e}")

        # 2. Event listener action scripts
        try:
            rows = await session.execute(
                text(
                    "SELECT id, name, action_config "
                    "FROM event_listeners "
                    "WHERE action_type = 'script'"
                )
            )
            for row in rows:
                config = (
                    row[2] if isinstance(row[2], dict) else json.loads(row[2] or "{}")
                )
                script_code = config.get("script_code", "")
                if script_code:
                    scripts.append({
                        "id": f"event_listener:{row[0]}",
                        "name": row[1],
                        "script": script_code,
                        "type": "action",
                        "is_condition": False,
                    })
        except Exception as e:
            logger.warning(f"Could not query event_listener actions: {e}")

        # 3. Schedule automation action scripts
        try:
            rows = await session.execute(
                text(
                    "SELECT id, name, action_config "
                    "FROM schedule_automations "
                    "WHERE action_type = 'script'"
                )
            )
            for row in rows:
                config = (
                    row[2] if isinstance(row[2], dict) else json.loads(row[2] or "{}")
                )
                script_code = config.get("script_code", "")
                if script_code:
                    scripts.append({
                        "id": f"schedule:{row[0]}",
                        "name": row[1],
                        "script": script_code,
                        "type": "scheduled_action",
                        "is_condition": False,
                    })
        except Exception as e:
            logger.warning(f"Could not query schedule_automations: {e}")

    await engine.dispose()
    return scripts


def print_report(results: list[dict]) -> None:
    """Print a compatibility report."""
    compatible = [r for r in results if r["compatible"]]
    incompatible = [r for r in results if not r["compatible"]]
    with_warnings = [r for r in results if r["warnings"]]

    print(f"\n{'=' * 60}")
    print("Monty Compatibility Report")
    print(f"{'=' * 60}")
    print(f"Total scripts:  {len(results)}")
    print(f"Compatible:     {len(compatible)}")
    print(f"Incompatible:   {len(incompatible)}")
    print(f"With warnings:  {len(with_warnings)}")

    if incompatible:
        print(f"\n{'─' * 60}")
        print("INCOMPATIBLE SCRIPTS")
        print(f"{'─' * 60}")
        for r in incompatible:
            print(f"\n  [{r['type']}] {r['id']}")
            print(f"  Error: {r['error']}")
            preview = r["script"][:200].replace("\n", "\\n")
            print(f"  Script: {preview}...")
            for w in r["warnings"]:
                print(f"  Warning: {w}")

    if with_warnings:
        print(f"\n{'─' * 60}")
        print("SCRIPTS WITH WARNINGS (but parseable)")
        print(f"{'─' * 60}")
        for r in with_warnings:
            if r["compatible"]:
                print(f"\n  [{r['type']}] {r['id']}")
                for w in r["warnings"]:
                    print(f"  Warning: {w}")

    if not incompatible and not with_warnings:
        print("\nAll scripts are compatible with Monty!")

    print()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check persisted Starlark scripts for Monty compatibility"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db"
        ),
        help="Database URL (default: $DATABASE_URL or sqlite:///family_assistant.db)",
    )
    args = parser.parse_args()

    print(f"Connecting to: {args.database_url.split('@')[-1]}")  # Hide credentials

    scripts = await fetch_scripts(args.database_url)

    if not scripts:
        print("\nNo persisted scripts found in database.")
        return

    print(f"Found {len(scripts)} persisted script(s). Checking compatibility...")

    results = []
    for s in scripts:
        result = check_script_compatibility(
            script=s["script"],
            script_id=f"{s['id']} ({s['name']})",
            script_type=s["type"],
            is_condition=s["is_condition"],
        )
        results.append(result)

    print_report(results)

    # Exit with error code if any incompatible
    incompatible = [r for r in results if not r["compatible"]]
    sys.exit(1 if incompatible else 0)


if __name__ == "__main__":
    asyncio.run(main())
