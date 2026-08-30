#!/usr/bin/env python3
"""Abstract message history into committable task templates for the review eval.

This is stage 1 of the history-derived pipeline described in
``docs/design/tool-call-review-eval.md``: walk historical turns, abstract each
tool call into an *enumerated* :class:`TaskTemplate` (intent, registry tool
names, argument *shapes* rather than values, sink class, taint tier,
content-kind tag), and run every template through the structural privacy
chokepoint (:meth:`TaskTemplate.validate_committable`) before it may be written.

The chokepoint fails closed: a template with any free-text or unrecognized
field aborts rather than being written, so private household text has no field
to travel in. Output is written only inside the repository's gitignored
``.review-eval-local/`` tree — the destination resolves through the shared
containment rule for household-derived eval material — and the script refuses
any other destination.

A row the abstraction cannot read — most plausibly a tool call whose recorded
arguments are not a JSON object at all — is *rejected* with a reason naming it,
alongside the templates the privacy chokepoint refuses. Coercing it to empty
arguments instead would emit a committable, well-formed-looking template
describing a task shape nothing recorded, biasing the quarry these templates
exist to describe and making the counts look sound.

Nothing here instantiates cases with content — stage 2 (a capable model
hallucinating concrete cases from committed templates) is a separate,
maintainer-run step. See ``docs/development/review-eval-history-extraction.md``.

The source database is only ever **read**, and ``--database-url`` is expected
to name the real one, so nothing here may write to it — under ``--dry-run``
least of all. Two things follow, and neither is the default path.

The schema is not initialized. The application's ``init_db`` runs ``alembic
upgrade head`` on a managed database and creates and stamps one that is not, so
calling it here would migrate a live deployment, or a copy kept deliberately at
its original revision. A database whose schema predates the tables this reads
fails on the query instead, which is the right outcome for a tool pointed at the
wrong place; this therefore needs a database already at a compatible revision,
not a fresh one.

The connection is read-only, and that is the rule the rest of this is an
instance of. A file-backed SQLite URL is rewritten to ``mode=ro``, so the driver
refuses every write this tool could attempt -- including one added later by
someone who did not read this docstring. On top of that the engine factory's
tuning pragmas are off, because its connect hook issues ``PRAGMA
journal_mode=WAL``, a persistent property of the file that would convert a
production database or an archival copy and leave ``-wal``/``-shm`` sidecars.
Without ``mode=ro`` a mistyped path would also have SQLite create the empty
database it was pointed at.

Usage:

    # Dry run against a dev SQLite DB: classify, validate, report, write nothing.
    python scripts/extract_review_history.py \
        --database-url "sqlite+aiosqlite:///family_assistant.db" --dry-run

    # Write committable templates into the private dir.
    python scripts/extract_review_history.py \
        --database-url "sqlite+aiosqlite:///family_assistant.db" \
        --out-dir .review-eval-local/templates
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml
from sqlalchemy.engine import make_url

from family_assistant.eval.private_paths import (
    PRIVATE_EVAL_DIR_NAME,
    PrivateEvalPathError,
    resolve_private_eval_path,
)
from family_assistant.eval.tool_call_review.schema import ToolResolutionError
from family_assistant.eval.tool_call_review.scrub import (
    TaskTemplate,
    TemplatePrivacyError,
    declared_argument_shapes,
)
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from family_assistant.storage.types import MessageHistoryRow


@dataclass(frozen=True, slots=True)
class _RejectedRow:
    """A history row that could not be abstracted, and why."""

    template_id: str
    reason: str


def _utc_datetime(raw: str) -> datetime:
    """Parse an ISO 8601 date or datetime and return it as an instant in UTC.

    ``message_history.timestamp`` is timezone-aware, and the boundary is
    resolved here rather than left to the driver, in both directions. A naive
    value is read as UTC, because comparing one against the column is an error
    on PostgreSQL and a silently wrong comparison on SQLite. An *offset* value
    is converted rather than passed through: SQLite does not preserve the
    offset, so ``2026-06-01T12:30:00-07:00`` would be compared as the wall clock
    ``12:30`` instead of the instant ``19:30``, quietly widening the window on
    one backend and not the other.
    """
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db"
        ),
        help="SQLAlchemy async URL (default: $DATABASE_URL or a dev sqlite file).",
    )
    parser.add_argument(
        "--out-dir",
        default=f"{PRIVATE_EVAL_DIR_NAME}/templates",
        help=(
            "Destination for committable templates. Must resolve inside the "
            f"repository's {PRIVATE_EVAL_DIR_NAME}/ tree — household-derived "
            "material never leaves the private tree."
        ),
    )
    parser.add_argument(
        "--interface-type",
        default=None,
        help="Restrict to one interface type (default: all).",
    )
    parser.add_argument(
        "--since",
        type=_utc_datetime,
        default=None,
        help=(
            "Only read messages at or after this ISO 8601 date or datetime, "
            "e.g. 2026-06-01. A bare date means midnight UTC; a naive datetime "
            "is read as UTC. Default: all history."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after abstracting this many committable templates.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify, validate, and report only; write nothing.",
    )
    return parser.parse_args(argv)


def _private_out_dir(raw_out_dir: str) -> Path:
    """Resolve the destination through the shared private-tree containment rule.

    The guard is structural, not advisory: the script has no committable output,
    so a destination outside the private tree is always a mistake and is refused
    before any read happens. Containment, not a name match, is what is checked,
    so a path that merely carries the marker name inside a tracked directory is
    refused here too.
    """
    try:
        return resolve_private_eval_path(raw_out_dir)
    except PrivateEvalPathError as exc:
        raise SystemExit(
            f"Refusing to write history-derived output: --out-dir {exc}. "
            "Household content must never leave the private tree."
        ) from exc


def _require_empty_out_dir(out_dir: Path) -> None:
    """Refuse to write templates beside the leftovers of an earlier extraction.

    The set this writes is the whole answer to one set of parameters, but it is
    written as one file per template into a shared directory, so a re-run after
    changing ``--interface-type`` or ``--limit`` leaves the previous run's
    templates sitting next to the new ones. The command then reports the count
    it just wrote while the directory holds more, and the two consumers — the
    maintainer skim, and the stage-2 pass that instantiates cases — read the
    whole directory, so both would work from task shapes this extraction did not
    produce.

    Refusing rather than clearing the directory: the skim is a human step, so
    the files here may be part-reviewed work, and deleting someone's working
    directory to save them one ``rm`` is the worse surprise. Checked before the
    database is read, so the refusal costs nothing.
    """
    if not out_dir.exists():
        return
    existing = sorted(entry.name for entry in out_dir.iterdir())
    if not existing:
        return
    shown = ", ".join(existing[:5])
    more = f" (and {len(existing) - 5} more)" if len(existing) > 5 else ""
    raise SystemExit(
        f"Refusing to write into a non-empty {out_dir}: it already holds "
        f"{len(existing)} entr{'y' if len(existing) == 1 else 'ies'} — {shown}"
        f"{more}. A template set describes one extraction, and files left from "
        "an earlier one would be read by the maintainer skim and by stage 2 as "
        "though this run had produced them. Remove the directory or name a "
        "different --out-dir."
    )


def _tool_call_arguments(raw: object) -> dict[str, object] | None:
    """Return the call's recorded argument object, or ``None`` if it is not one."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _row_taint_tier(row: MessageHistoryRow) -> str:
    metadata = row.get("taint_metadata")
    if isinstance(metadata, dict):
        tier = metadata.get("max_tier")
        if isinstance(tier, str):
            return tier
    return "<unknown>"


def _templates_from_rows(
    rows: list[MessageHistoryRow],
    *,
    interface_type: str,
    conversation_id: str,
) -> Iterator[TaskTemplate | _RejectedRow]:
    """Yield one abstracted template, or one rejection, per recorded tool call.

    Only schema-declared argument keys are read, with the shape taken from the
    tool's parameter schema; values and undeclared keys never enter the template,
    so household text riding in an unexpected argument key cannot cross the
    privacy boundary. Intent and content-kind are not recoverable from history
    alone, so they are emitted as a placeholder / ``none`` for a later
    classification pass to refine. A template whose tool no longer resolves gets
    empty shapes and is rejected downstream by the validator; a call whose
    recorded arguments are not a JSON object is rejected here, because the
    template it would otherwise produce describes argument shapes that were
    never observed.
    """
    for index, row in enumerate(rows):
        if row.get("role") != "assistant":
            continue
        tool_calls = row.get("tool_calls")
        if not tool_calls:
            continue
        tier = _row_taint_tier(row)
        for call_index, tool_call in enumerate(tool_calls):
            name = tool_call.function.name
            template_id = _template_id(
                interface_type, conversation_id, index, call_index, name
            )
            arguments = _tool_call_arguments(tool_call.function.arguments)
            if arguments is None:
                yield _RejectedRow(
                    template_id=template_id,
                    reason=(
                        f"malformed arguments: {interface_type}/{conversation_id} "
                        f"row {index} call {call_index} ({name}) recorded "
                        "tool-call arguments that are not a JSON object"
                    ),
                )
                continue
            try:
                argument_shapes = declared_argument_shapes(name, arguments)
            except ToolResolutionError:
                # An unresolved tool has no schema to derive shapes from; leave
                # them empty and let the validator reject the tool name.
                argument_shapes = {}
            yield TaskTemplate(
                template_id=template_id,
                boundary="conversation",
                intent_category="<unknown>",
                tool_names=[name],
                argument_shapes=argument_shapes,
                sink_class="<unknown>",
                taint_tier=tier,
                content_kind="none",
            )


def _template_id(
    interface_type: str,
    conversation_id: str,
    row_index: int,
    call_index: int,
    tool_name: str,
) -> str:
    seed = "\x1f".join([
        interface_type,
        conversation_id,
        str(row_index),
        str(call_index),
        tool_name,
    ])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"tmpl-{digest}"


def _partition_templates(
    grouped: Mapping[tuple[str, str], list[MessageHistoryRow]],
    *,
    limit: int | None,
) -> tuple[list[TaskTemplate], list[tuple[str, str]]]:
    """Split abstracted history into committable templates and rejections.

    Rejections are one list whatever their cause — a row that could not be
    abstracted, or a template the privacy chokepoint refused — so the reported
    counts account for every tool call the history held.
    """
    committable: list[TaskTemplate] = []
    rejected: list[tuple[str, str]] = []
    for (iface, conversation_id), rows in grouped.items():
        for abstracted in _templates_from_rows(
            rows, interface_type=iface, conversation_id=conversation_id
        ):
            if isinstance(abstracted, _RejectedRow):
                rejected.append((abstracted.template_id, abstracted.reason))
                continue
            try:
                abstracted.validate_committable()
            except TemplatePrivacyError as error:
                rejected.append((abstracted.template_id, str(error)))
                continue
            committable.append(abstracted)
            if limit is not None and len(committable) >= limit:
                return committable, rejected
    return committable, rejected


def _read_only_url(database_url: str) -> str:
    """Rewrite a file-backed SQLite URL to open read-only.

    Turning the tuning pragmas off stopped this rewriting the file it reads, but
    not opening it for writing: SQLite *creates* a database that is not there,
    so a typo in ``--database-url`` left a new empty file behind before the
    query failed, under ``--dry-run`` too. Read-only mode is the rule the
    earlier no-init and no-pragmas fixes were each an instance of -- the driver
    refuses any write this tool could attempt, including one added later by
    someone who did not read this docstring.

    The URL is parsed and rebuilt rather than concatenated. Appending
    ``?mode=ro&uri=true`` to a URL that already carries parameters produces a
    second ``?``, which SQLAlchemy folds into the preceding value
    (``timeout=5?mode=ro``) and then sees no ``mode`` at all -- the protection
    silently absent exactly where a URL was most deliberately written.

    Only file-backed SQLite is rewritten. ``:memory:`` has nothing to protect,
    a URL already in URI form is left as the caller wrote it (see the known
    limitation in the extraction runbook), and PostgreSQL grants read-only
    access through its own role system rather than the URL.
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return database_url
    database = url.database
    if not database or database.startswith("file:") or database == ":memory:":
        return database_url
    return str(
        url.set(database=f"file:{database}").update_query_dict({
            "mode": "ro",
            "uri": "true",
        })
    )


async def _collect_templates(
    database_url: str,
    *,
    interface_type: str | None,
    limit: int | None,
    since: datetime | None = None,
) -> tuple[list[TaskTemplate], list[tuple[str, str]]]:
    engine = create_engine_with_sqlite_optimizations(
        _read_only_url(database_url), apply_sqlite_pragmas=False
    )
    try:
        db = Database(engine)
        grouped = await db.message_history.get_all_grouped(
            interface_type=interface_type,
            date_from=since,
            include_subconversations=True,
        )
    finally:
        await engine.dispose()
    return _partition_templates(grouped, limit=limit)


def _write_templates(templates: list[TaskTemplate], out_dir: Path) -> None:
    """Write one file per template into a directory :func:`main` proved empty."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for template in templates:
        # Revalidate at the write boundary: nothing reaches disk without passing
        # the fail-closed chokepoint immediately before it is written.
        template.validate_committable()
        path = out_dir / f"{template.template_id}.yaml"
        path.write_text(
            yaml.safe_dump(template.model_dump(mode="json"), sort_keys=False),
            encoding="utf-8",
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = _private_out_dir(args.out_dir)
    if not args.dry_run:
        # Before the read, not after: a refusal that arrives once the database
        # has been walked has cost the maintainer the whole extraction.
        _require_empty_out_dir(out_dir)

    committable, rejected = asyncio.run(
        _collect_templates(
            args.database_url,
            interface_type=args.interface_type,
            limit=args.limit,
            since=args.since,
        )
    )

    print(f"Committable templates: {len(committable)}")
    print(f"Rejected: {len(rejected)}")
    for template_id, reason in rejected[:20]:
        print(f"  - {template_id}: {reason}")

    if args.dry_run:
        print("Dry run: no files written.")
        return 0

    _write_templates(committable, out_dir)
    print(f"Wrote {len(committable)} templates to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
