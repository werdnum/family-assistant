#!/usr/bin/env python3
"""Stamp provenance on note rows written before provenance stamping existed.

A note reaches a prompt unasked only when its stored tier is admissible for
reuse, and a row with no envelope reads as ``unknown_external``. Run this once
at rollout so the pre-stamping corpus does not drop out of every prompt or
taint every turn that lists it.

Rows the data identifies as external (call transcripts) and rows you name are
stamped ``unknown_external``; every other unstamped row is stamped
``trusted_internal`` -- a judgment that it is household material. Name the
workspace imports you know about with ``--exclude-title`` or
``--exclude-title-pattern``. Rows that already carry an envelope are never
touched.

See docs/design/ambient-note-admission-at-write-time.md, "Existing rows".

Usage:

    # What would be stamped, and how? Nothing is written.
    python scripts/restamp_note_provenance.py --database-url "$DATABASE_URL"

    # Stamp it, naming two known imports as external.
    python scripts/restamp_note_provenance.py --database-url "$DATABASE_URL" \\
        --exclude-title "Imported research" --exclude-title-pattern "import-*" \\
        --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from family_assistant.security.note_restamp import (
    RestampExclusions,
    apply_note_restamp,
    plan_note_restamp,
)
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import Database


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db"
        ),
        help="SQLAlchemy async URL (default: $DATABASE_URL or a dev sqlite file).",
    )
    parser.add_argument(
        "--exclude-title",
        action="append",
        default=[],
        help="Stamp this note unknown_external; repeatable.",
    )
    parser.add_argument(
        "--exclude-title-pattern",
        action="append",
        default=[],
        help="Stamp notes whose title matches this glob unknown_external; repeatable.",
    )
    parser.add_argument(
        "--transcript-label",
        action="append",
        default=[],
        help=(
            "A visibility label the call-transcript profile applies; a note "
            "carrying all of them is a transcript. Repeatable."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the stamps. Without it, the plan is listed and nothing is written.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    engine = create_engine_with_sqlite_optimizations(args.database_url)
    try:
        db = Database(engine)
        exclusions = RestampExclusions(
            titles=frozenset(args.exclude_title),
            title_patterns=tuple(args.exclude_title_pattern),
            transcript_labels=frozenset(args.transcript_label),
        )
        decisions = await plan_note_restamp(db, exclusions)
        for decision in decisions:
            print(
                f"{decision.note.id}\t{decision.tier.config_value}\t"
                f"{decision.rule.value}\t{decision.note.title}"
            )
        print(f"{len(decisions)} note(s) without provenance.", file=sys.stderr)
        if not args.apply:
            print("Dry run; pass --apply to stamp them.", file=sys.stderr)
            return 0
        applied = await apply_note_restamp(db, decisions)
        print(f"Stamped {len(applied)} note(s).", file=sys.stderr)
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
