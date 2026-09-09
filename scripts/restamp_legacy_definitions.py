#!/usr/bin/env python3
"""Amnesty executable definitions that predate taint provenance stamping.

An automation, event listener, or stored script written before definition
records existed carries none, and a firing reads that absence as fail-closed:
the definition renders to the tool-call reviewer as a stub and seeds its turn at
``unknown_external``, permanently, because nothing about a legacy definition
changes with time. For a deployment whose whole automation estate predates the
feature, that is confirmation fatigue rather than protection.

This is the bulk migration path. It lists every definition holding no record
that was created before a cutoff the operator states, and -- only with
``--apply`` -- records an amnesty for each: an honest ``unknown_external``
authoring stamp (the authoring turn really is unknown; nothing here invents a
trusted one) with a disposition saying an operator, not a gate, let this
through. Resolution then cures it exactly as it cures a judge-allowed creation,
and the reviewer is told which of the two it is reading.

Three properties are worth knowing before running it:

* **It fills absence only.** A definition already holding a record -- cured,
  uncured, or void through a hash mismatch -- is never touched. A mismatch in
  particular is content that changed under a real record, which is what the
  fail-closed default exists for.
* **The grant binds to content.** The record hashes the definition as it stands
  in the writing transaction, so any later edit voids the amnesty and re-enters
  the ordinary creation gate.
* **It is reversible.** ``--revoke`` clears records carrying the amnesty
  disposition and nothing else, restoring the pre-restamp state exactly.

One-shot callbacks in flight (reminders, future callbacks) are out of scope:
their records ride an enqueued payload and expire on firing.

See ``docs/design/legacy-definition-amnesty.md``.

Usage:

    # What would be amnestied? Nothing is written.
    python scripts/restamp_legacy_definitions.py \\
        --database-url "$DATABASE_URL" --created-before 2026-08-01

    # Grant it, for automations only.
    python scripts/restamp_legacy_definitions.py \\
        --database-url "$DATABASE_URL" --created-before 2026-08-01 \\
        --kind schedule_automation --apply

    # List what currently holds an amnesty, then take it back.
    python scripts/restamp_legacy_definitions.py --database-url "$DATABASE_URL" --revoke
    python scripts/restamp_legacy_definitions.py --database-url "$DATABASE_URL" \\
        --revoke --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

from family_assistant.security.definition_amnesty import (
    AMNESTIABLE_ARTIFACT_KINDS,
    LegacyDefinition,
    amnesty_legacy_definitions,
    list_amnestied_definitions,
    list_legacy_definitions,
    revoke_definition_amnesty,
)
from family_assistant.security.definition_records import DefinitionArtifactKind
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import Database


def _utc_datetime(raw: str) -> datetime:
    """Parse an ISO 8601 date or datetime as an instant in UTC.

    A bare date means midnight UTC and a naive datetime is read as UTC, because
    comparing a naive value against a timezone-aware column errors on PostgreSQL
    and compares wrongly on SQLite. An offset value is converted rather than
    passed through, for the same reason.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not an ISO 8601 date or datetime (e.g. 2026-08-01)."
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
        "--created-before",
        type=_utc_datetime,
        default=None,
        help=(
            "Only definitions created strictly before this ISO 8601 date or "
            "datetime are eligible. State the instant definition records were "
            "deployed: a definition written after it with no record is a "
            "write-path regression, not a legacy artifact. Required unless "
            "--revoke."
        ),
    )
    parser.add_argument(
        "--kind",
        action="append",
        choices=[kind.value for kind in AMNESTIABLE_ARTIFACT_KINDS],
        default=None,
        help="Restrict to one definition class; repeatable. Default: all three.",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=None,
        help="Restrict to definitions with this name; repeatable.",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help=(
            "Act on the definitions currently holding an amnesty, clearing it "
            "and restoring their fail-closed state, instead of granting one."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the change. Without it, the selection is listed and nothing is written.",
    )
    return parser.parse_args(argv)


def _selected_kinds(raw: list[str] | None) -> tuple[DefinitionArtifactKind, ...]:
    if not raw:
        return AMNESTIABLE_ARTIFACT_KINDS
    return tuple(DefinitionArtifactKind(value) for value in raw)


def _by_name(
    definitions: list[LegacyDefinition],
    names: list[str] | None,
) -> list[LegacyDefinition]:
    if not names:
        return definitions
    wanted = set(names)
    return [definition for definition in definitions if definition.name in wanted]


async def _run(args: argparse.Namespace) -> int:
    engine = create_engine_with_sqlite_optimizations(args.database_url)
    try:
        db = Database(engine)
        kinds = _selected_kinds(args.kind)
        if args.revoke:
            selected = _by_name(
                await list_amnestied_definitions(db, kinds=kinds), args.name
            )
            verb, past = "revoke the amnesty of", "Revoked"
        else:
            selected = _by_name(
                await list_legacy_definitions(
                    db, created_before=args.created_before, kinds=kinds
                ),
                args.name,
            )
            verb, past = "amnesty", "Amnestied"

        for definition in selected:
            print(f"  {definition.describe()}")
        if not selected:
            print("Nothing selected.")
            return 0
        if not args.apply:
            print(
                f"\nWould {verb} {len(selected)} definition(s). "
                "Re-run with --apply to write."
            )
            return 0

        if args.revoke:
            changed = [
                definition
                for definition in selected
                if await revoke_definition_amnesty(db, definition)
            ]
        else:
            changed = await amnesty_legacy_definitions(
                db, selected, created_before=args.created_before
            )
    finally:
        await engine.dispose()

    print(f"\n{past} {len(changed)} of {len(selected)} definition(s).")
    if len(changed) != len(selected):
        # Not an error: the write re-checks eligibility under its own lock, so a
        # definition written through a real gate since the listing keeps that
        # record rather than being overwritten by this one.
        print(
            "Definitions that changed between the listing and the write were "
            "skipped, leaving their newer records standing."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.created_before is None and not args.revoke:
        # Refused rather than defaulted: a cutoff nobody stated would amnesty
        # definitions written after stamping shipped, which are write-path
        # regressions to fix rather than legacy artifacts to bless.
        print(
            "--created-before is required: state the instant definition "
            "records were deployed.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
