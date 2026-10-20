#!/usr/bin/env python3
"""Export historical assistant turns and a stratified candidate list.

Milestone 1 of ``docs/design/workhorse-eval-set.md``. Reads message history,
flattens each turn of the named profiles into a record, classifies it into a
stratum, and writes three files into the private tree:

- ``records.jsonl`` -- every extracted turn, with prompt, tool calls and
  results, reply, usage, and the weak outcome signals.
- ``summary.json`` -- the parameters, population counts, and each stratum's
  frequency, which is what results are later weighted back to.
- ``candidates.yaml`` -- the deterministic stratified sample curation starts
  from, each entry carrying its stratum weight.
- ``google_world.json`` -- every Gmail message and Drive file a recorded turn
  saw, pooled into the world the sandbox's fake Google serves.

Everything written is raw household text, so the destination must resolve
inside the repository's gitignored private tree. The database is opened
read-only and never initialised; see :mod:`family_assistant.eval.history_access`.

Usage:

    python scripts/extract_workhorse_turns.py \\
        --database-url "$DATABASE_URL" --since 2026-04-23
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml

from family_assistant.eval.history_access import open_read_only_engine
from family_assistant.eval.private_paths import (
    PRIVATE_EVAL_DIR_NAME,
    PrivateEvalPathError,
    resolve_private_eval_path,
)
from family_assistant.eval.workhorse.google_world import build_google_world
from family_assistant.eval.workhorse.strata import (
    DEFAULT_TEMPLATED_PREFIXES,
    sample_candidates,
    stratum_frequencies,
    stratum_of,
)
from family_assistant.eval.workhorse.turn_records import build_turn_records
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from family_assistant.eval.workhorse.google_world import GoogleWorld
    from family_assistant.eval.workhorse.strata import SampledTurn
    from family_assistant.eval.workhorse.turn_records import TurnRecord
    from family_assistant.storage.types import MessageHistoryRow

DEFAULT_PROFILES = ("default_assistant", "complex_tasks")
EXCERPT_CHARS = 280


def _utc_datetime(raw: str) -> datetime:
    """Parse an ISO 8601 date or datetime as an instant in UTC (naive = UTC)."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not an ISO 8601 date or datetime (e.g. 2026-06-01)."
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db"
        ),
        help="SQLAlchemy async URL (default: $DATABASE_URL or a dev sqlite file).",
    )
    parser.add_argument(
        "--out-dir",
        default=f"{PRIVATE_EVAL_DIR_NAME}/workhorse/turns",
        help=f"Destination; must resolve inside {PRIVATE_EVAL_DIR_NAME}/.",
    )
    parser.add_argument(
        "--since",
        type=_utc_datetime,
        default=None,
        help="Only read history at or after this ISO 8601 date or datetime.",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=None,
        help=(
            "Processing profile to extract; repeatable "
            f"(default: {', '.join(DEFAULT_PROFILES)})."
        ),
    )
    parser.add_argument(
        "--templated-prefix",
        action="append",
        default=None,
        help=(
            "Prompt prefix marking a templated automation turn; repeatable. "
            "Replaces the built-in list when given."
        ),
    )
    parser.add_argument(
        "--target",
        type=int,
        default=60,
        help="Human-authored candidates to sample (default: 60).",
    )
    parser.add_argument(
        "--templated-per-template",
        type=int,
        default=2,
        help="Candidates to sample from each templated automation (default: 2).",
    )
    parser.add_argument(
        "--negative-share",
        type=float,
        default=1 / 3,
        help="Most of a stratum's picks given to negative-signal turns.",
    )
    parser.add_argument(
        "--seed",
        default="workhorse",
        help="Sampling seed; the same snapshot and seed give the same sample.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and report only; write nothing.",
    )
    return parser.parse_args(argv)


def _private_out_dir(raw_out_dir: str) -> Path:
    try:
        return resolve_private_eval_path(raw_out_dir)
    except PrivateEvalPathError as exc:
        raise SystemExit(
            f"Refusing to write history-derived output: --out-dir {exc}. "
            "Household content must never leave the private tree."
        ) from exc


def _require_empty_out_dir(out_dir: Path) -> None:
    """Refuse to mix this extraction with files left by an earlier one.

    The candidate list is curated by hand, so a non-empty directory may hold
    part-reviewed work; refusing costs one ``rm`` where clearing it could
    cost the curation.
    """
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(
            f"Refusing to write into a non-empty {out_dir}. Remove it or name a "
            "different --out-dir."
        )


async def _read_history(
    database_url: str, *, since: datetime | None
) -> dict[tuple[str, str], list[MessageHistoryRow]]:
    # A cold import of the storage repositories enters an import cycle at
    # observability.metrics -> llm -> tools -> plugins -> llm.typesafe ->
    # observability.metrics and fails; initialising the llm package first
    # enters the cycle from the end that completes.
    importlib.import_module("family_assistant.llm")
    engine = open_read_only_engine(database_url)
    try:
        return await Database(engine).message_history.get_all_grouped(
            date_from=since,
            include_subconversations=True,
            # Scheduled callbacks and delegation wakes open their turns with
            # an internal user row; without these they vanish from the record.
            include_internal=True,
        )
    finally:
        await engine.dispose()


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_CHARS else f"{flat[:EXCERPT_CHARS]}…"


def _summary(
    args: argparse.Namespace,
    *,
    profiles: Sequence[str],
    prefixes: Sequence[str],
    records: Sequence[TurnRecord],
    sampled: Sequence[SampledTurn],
    world: GoogleWorld,
) -> dict[str, object]:
    return {
        "extracted_at": datetime.now(UTC).isoformat(),
        "since": args.since.isoformat() if args.since else None,
        "profiles": list(profiles),
        "templated_prefixes": list(prefixes),
        "seed": args.seed,
        "target": args.target,
        "templated_per_template": args.templated_per_template,
        "negative_share": args.negative_share,
        "turns": len(records),
        "turns_by_population": dict(Counter(r.population for r in records)),
        "turns_by_profile": dict(Counter(r.profile for r in records)),
        "negative_signal_turns": sum(1 for r in records if r.negative_signal),
        "candidates": len(sampled),
        "google_world": {
            "messages": len(world.messages),
            "messages_with_body": sum(1 for m in world.messages if m.body is not None),
            "files": len(world.files),
            "files_with_content": sum(1 for f in world.files if f.content is not None),
        },
        "stratum_frequencies": stratum_frequencies(records),
    }


def _candidate_entries(
    records: Sequence[TurnRecord], sampled: Sequence[SampledTurn]
) -> list[dict[str, object]]:
    by_id = {record.turn_id: record for record in records}
    entries: list[dict[str, object]] = []
    for pick in sampled:
        record = by_id[pick.turn_id]
        entries.append({
            "turn_id": record.turn_id,
            "stratum": pick.stratum,
            "weight": round(pick.weight, 3),
            "asked_at": record.asked_at.isoformat(),
            "profile": record.profile,
            "intent": record.intent,
            "prompt": _excerpt(record.prompt),
            "tool_signature": record.tool_signature,
            "tool_calls": len(record.tool_calls),
            "delegated": record.delegated,
            "attachments": record.attachment_count,
            "signals": {
                "errors": len(record.errors),
                "correction_shaped_followup": record.correction_shaped_followup,
                "escalated": record.escalated,
                "followup_within_10m": record.followup_within_10m,
            },
            "multi_turn_context": record.previous_turn_id is not None
            and (record.seconds_to_previous_turn or 0) < 3600,
            "decision": None,
        })
    return entries


def _write(
    out_dir: Path,
    *,
    records: Sequence[TurnRecord],
    summary: dict[str, object],
    candidates: list[dict[str, object]],
    world: GoogleWorld,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(record.model_dump_json())
            handle.write("\n")
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "google_world.json").write_text(
        world.model_dump_json(indent=1), encoding="utf-8"
    )
    (out_dir / "candidates.yaml").write_text(
        yaml.safe_dump(candidates, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = _private_out_dir(args.out_dir)
    if not args.dry_run:
        _require_empty_out_dir(out_dir)
    profiles = tuple(args.profile or DEFAULT_PROFILES)
    prefixes = tuple(args.templated_prefix or DEFAULT_TEMPLATED_PREFIXES)

    # Every pre-taint row warns once as it is read, which buries the report in
    # thousands of lines; the extraction does not use taint metadata.
    logging.getLogger("family_assistant.storage.repositories.message_history").setLevel(
        logging.ERROR
    )
    grouped = asyncio.run(_read_history(args.database_url, since=args.since))
    records = build_turn_records(
        grouped, profiles=profiles, templated_prefixes=prefixes
    )
    sampled = sample_candidates(
        records,
        target=args.target,
        templated_per_template=args.templated_per_template,
        negative_share=args.negative_share,
        seed=args.seed,
    )
    world = build_google_world(grouped)
    summary = _summary(
        args,
        profiles=profiles,
        prefixes=prefixes,
        records=records,
        sampled=sampled,
        world=world,
    )

    print(f"Turns extracted: {len(records)} {summary['turns_by_population']}")
    print(f"Strata: {len({stratum_of(r).key for r in records})}")
    print(f"Candidates sampled: {len(sampled)}")

    if args.dry_run:
        print("Dry run: no files written.")
        return 0

    _write(
        out_dir,
        records=records,
        summary=summary,
        candidates=_candidate_entries(records, sampled),
        world=world,
    )
    print(f"Wrote records, summary, candidates and the Google world to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
