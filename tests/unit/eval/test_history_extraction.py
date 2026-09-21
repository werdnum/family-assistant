"""Unit tests for the offline history-extraction path of the review-eval harness."""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.eval import private_paths
from family_assistant.eval.private_paths import (
    PrivateEvalPathError,
    resolve_private_eval_path,
)
from family_assistant.eval.tool_call_review.registry_snapshot import (
    descriptors_to_snapshot,
    snapshot_to_registry,
)
from family_assistant.eval.tool_call_review.scrub import TaskTemplate
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.paths import PROJECT_ROOT
from family_assistant.storage.types import MessageHistoryRow
from family_assistant.tools.metadata import ToolDescriptor, ToolTag

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from types import ModuleType

    from family_assistant.security.taint import TaintMetadata

pytestmark = pytest.mark.no_db

_TRUSTED = {
    "version": "runtime_v2",
    "max_tier": "trusted_user",
    "history_high_taint_present": False,
    "fresh_high_taint_seen_at_sequence": None,
    "sources": [],
    "approved_sinks": [],
}


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("templates", id="no-marker"),
        pytest.param(".review-eval-local/../templates", id="traversal"),
        # `.gitignore` ignores only the repository-root `.review-eval-local/`,
        # so a nested one is a *tracked* directory wearing the marker name.
        pytest.param("nested/.review-eval-local/templates", id="nested-marker"),
    ],
)
def test_private_eval_path_rejects_anything_outside_the_ignored_tree(
    path: str,
) -> None:
    with pytest.raises(PrivateEvalPathError, match=".review-eval-local"):
        resolve_private_eval_path(path)


def test_private_eval_path_accepts_a_root_anchored_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anchoring is at the repository root, whatever the process's cwd is."""
    monkeypatch.chdir(tmp_path)
    resolved = resolve_private_eval_path(".review-eval-local/templates")
    assert resolved == PROJECT_ROOT / ".review-eval-local" / "templates"


def test_private_eval_path_rejects_a_symlink_out_of_the_private_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lexical check calls this contained; the write would follow the link out."""
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", tmp_path)
    tracked = tmp_path / "src" / "datasets"
    tracked.mkdir(parents=True)
    private_root = tmp_path / ".review-eval-local"
    private_root.mkdir()
    (private_root / "runs").symlink_to(tracked)

    with pytest.raises(PrivateEvalPathError, match=".review-eval-local"):
        resolve_private_eval_path(".review-eval-local/runs")


def test_private_eval_path_rejects_a_symlinked_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked marker makes containment vacuous, not merely inaccurate.

    Candidate and root both resolve under the link's target, so every write
    passes the check on its way into a tracked directory.
    """
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", tmp_path)
    tracked = tmp_path / "src" / "datasets"
    tracked.mkdir(parents=True)
    (tmp_path / ".review-eval-local").symlink_to(tracked)

    with pytest.raises(PrivateEvalPathError, match="is a symlink"):
        resolve_private_eval_path(".review-eval-local/runs")


def test_private_eval_path_accepts_a_real_path_under_the_private_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".review-eval-local" / "runs").mkdir(parents=True)

    resolved = resolve_private_eval_path(".review-eval-local/runs/today.jsonl")

    assert resolved == tmp_path / ".review-eval-local" / "runs" / "today.jsonl"


def test_private_eval_path_accepts_a_repository_reached_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving only the candidate would reject every path in such a checkout.

    A guard that refuses ordinary use gets turned off, so the private root is
    resolved the same way the candidate is.
    """
    real_root = tmp_path / "real-checkout"
    (real_root / ".review-eval-local").mkdir(parents=True)
    linked_root = tmp_path / "linked-checkout"
    linked_root.symlink_to(real_root)
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", linked_root)

    assert (
        resolve_private_eval_path(".review-eval-local/runs")
        == real_root / ".review-eval-local" / "runs"
    )
    assert (
        resolve_private_eval_path(linked_root / ".review-eval-local" / "runs")
        == real_root / ".review-eval-local" / "runs"
    )


def _history_row(
    *,
    role: str = "assistant",
    tool_calls: list[ToolCallItem] | None = None,
) -> MessageHistoryRow:
    return MessageHistoryRow(
        internal_id=1,
        interface_type="telegram",
        conversation_id="chat-1",
        interface_message_id=None,
        turn_id=None,
        thread_root_id=None,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        role=role,
        content=None,
        tool_calls=tool_calls,
        reasoning_info=None,
        tool_call_id=None,
        error_traceback=None,
        processing_profile_id=None,
        subconversation_id=None,
        user_id=None,
        attachments=None,
        tool_name=None,
        provider_metadata=None,
        taint_metadata_json=None,
        taint_metadata_version=None,
        taint_metadata=cast("TaintMetadata", _TRUSTED),
        is_internal=False,
    )


def _tool_call(arguments: str | dict[str, object]) -> ToolCallItem:
    return ToolCallItem(
        id="call-1",
        type="function",
        function=ToolCallFunction(name="send_message_to_user", arguments=arguments),
    )


@pytest.mark.parametrize(
    "arguments",
    ['{"target_chat_id": "chat-1", "message_c', "[1,2]", "null"],
    ids=["truncated", "json-array", "json-null"],
)
def test_history_extraction_rejects_malformed_tool_arguments(arguments: str) -> None:
    """Arguments that are not a JSON object reject the row, never coerce to ``{}``.

    Coercion emitted a committable template with no argument shapes, so a
    corrupt history row became a well-formed-looking record of a task shape
    nothing observed, and the dry-run counts called it valid.
    """
    script = _load_history_extraction_script()

    abstracted = list(
        script._templates_from_rows(
            [_history_row(tool_calls=[_tool_call(arguments)])],
            interface_type="telegram",
            conversation_id="chat-1",
        )
    )

    assert len(abstracted) == 1
    rejection = abstracted[0]
    assert isinstance(rejection, script._RejectedRow)
    assert "malformed arguments" in rejection.reason
    assert "telegram/chat-1 row 0 call 0 (send_message_to_user)" in rejection.reason


def test_history_extraction_abstracts_a_well_formed_row() -> None:
    script = _load_history_extraction_script()

    abstracted = list(
        script._templates_from_rows(
            [
                _history_row(
                    tool_calls=[
                        _tool_call(
                            '{"target_chat_id": "chat-1", "message_content": "hi"}'
                        )
                    ]
                )
            ],
            interface_type="telegram",
            conversation_id="chat-1",
        )
    )

    assert len(abstracted) == 1
    template = abstracted[0]
    assert isinstance(template, TaskTemplate)
    assert template.tool_names == ["send_message_to_user"]
    assert template.argument_shapes == {
        "target_chat_id": "string",
        "message_content": "string",
    }
    template.validate_committable()


def test_history_extraction_counts_report_malformed_rows_as_rejected() -> None:
    """The reported counts account for the corrupt row instead of hiding it."""
    script = _load_history_extraction_script()
    rows = [
        _history_row(
            tool_calls=[_tool_call('{"target_chat_id": "chat-1"}')],
        ),
        _history_row(tool_calls=[_tool_call('{"target_chat_id": ')]),
    ]

    committable, rejected = script._partition_templates(
        {("telegram", "chat-1"): rows}, limit=None
    )

    assert len(committable) == 1
    assert len(rejected) == 1
    template_id, reason = rejected[0]
    assert template_id.startswith("tmpl-")
    assert "malformed arguments" in reason


def test_history_export_uses_the_shared_containment_rule() -> None:
    """The template export refuses anything the shared containment rule refuses.

    The finding this covers is that a marker-name check accepted
    ``nested/.review-eval-local/templates``, which `.gitignore` tracks; every
    writer resolves through one rule, so none can drift from it.
    """
    script = _load_history_extraction_script()
    with pytest.raises(SystemExit, match=".review-eval-local"):
        script._private_out_dir("nested/.review-eval-local/templates")
    assert (
        script._private_out_dir(".review-eval-local/templates")
        == PROJECT_ROOT / ".review-eval-local" / "templates"
    )


async def test_history_extraction_never_migrates_the_source_database(
    tmp_path: Path,
) -> None:
    """Reading history must not create or upgrade the schema it reads.

    ``--database-url`` is pointed at the real database, so calling the
    application's ``init_db`` here would run ``alembic upgrade head`` against a
    live deployment — or create and stamp a schema on a copy deliberately kept
    at an older revision — and would do it under ``--dry-run`` too. An empty
    database must therefore fail on the query and be left exactly as it was.
    """
    script = _load_history_extraction_script()
    db_path = tmp_path / "empty.db"
    db_path.touch()

    with pytest.raises(Exception, match="message_history"):
        await script._collect_templates(
            f"sqlite+aiosqlite:///{db_path}", interface_type=None, limit=None
        )

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables == set()


async def test_history_extraction_leaves_the_sqlite_journal_mode_alone(
    tmp_path: Path,
) -> None:
    """Reading must not convert the source file to WAL.

    ``create_engine_with_sqlite_optimizations`` is the application's engine and
    its connect hook issues ``PRAGMA journal_mode=WAL``, which is a persistent
    property of the file and leaves ``-wal``/``-shm`` sidecars. Pointed at a
    production database or an archival copy, a reader that used it would change
    the file's storage mode.
    """
    script = _load_history_extraction_script()
    db_path = tmp_path / "delete-mode.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")

    with pytest.raises(Exception, match="message_history"):
        await script._collect_templates(
            f"sqlite+aiosqlite:///{db_path}", interface_type=None, limit=None
        )

    with sqlite3.connect(db_path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"


async def test_history_extraction_does_not_create_a_mistyped_database(
    tmp_path: Path,
) -> None:
    """A typo in --database-url must not leave a new database behind.

    SQLite creates a file-backed database that is not there, so an ordinary
    read/write URL turned a mistyped path into an empty file before the query
    failed — under --dry-run as well. Read-only mode is the rule that the
    earlier no-init and no-pragmas fixes were each one instance of.
    """
    script = _load_history_extraction_script()
    missing = tmp_path / "typo.db"

    with pytest.raises(Exception, match="unable to open database file"):
        await script._collect_templates(
            f"sqlite+aiosqlite:///{missing}", interface_type=None, limit=None
        )

    assert not missing.exists()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "sqlite+aiosqlite:///family.db",
            "sqlite+aiosqlite:///file:family.db?mode=ro&uri=true",
            id="file-backed",
        ),
        pytest.param(
            # Concatenating produced a second "?", which SQLAlchemy folded into
            # the preceding value (timeout=5?mode=ro) leaving no mode at all —
            # the protection silently absent exactly where a URL was most
            # deliberately written.
            "sqlite+aiosqlite:///family.db?timeout=5",
            "sqlite+aiosqlite:///file:family.db?mode=ro&timeout=5&uri=true",
            id="existing-query-parameters-preserved",
        ),
        pytest.param(
            "sqlite+aiosqlite:///:memory:",
            "sqlite+aiosqlite:///:memory:",
            id="memory-untouched",
        ),
        pytest.param(
            "postgresql+asyncpg://host/db",
            "postgresql+asyncpg://host/db",
            id="postgres-untouched",
        ),
        pytest.param(
            "sqlite+aiosqlite:///file:x.db?mode=rw&uri=true",
            "sqlite+aiosqlite:///file:x.db?mode=rw&uri=true",
            id="already-uri-left-as-written",
        ),
    ],
)
def test_read_only_url_rewrites_only_file_backed_sqlite(
    url: str, expected: str
) -> None:
    script = _load_history_extraction_script()
    assert script._read_only_url(url) == expected


def test_a_nonempty_output_directory_is_refused(tmp_path: Path) -> None:
    """Leftovers from an earlier extraction must not be written beside new ones.

    One file per template into a shared directory means a re-run after changing
    --interface-type or --limit leaves the previous set in place, while the
    command reports only the count it just wrote. Both consumers — the
    maintainer skim and stage 2 — read the whole directory.
    """
    script = _load_history_extraction_script()
    out_dir = tmp_path / "templates"
    out_dir.mkdir()
    (out_dir / "tmpl-from-an-earlier-run.yaml").write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="non-empty"):
        script._require_empty_out_dir(out_dir)


def test_an_empty_or_absent_output_directory_is_accepted(tmp_path: Path) -> None:
    script = _load_history_extraction_script()

    script._require_empty_out_dir(tmp_path / "does-not-exist")
    empty = tmp_path / "empty"
    empty.mkdir()
    script._require_empty_out_dir(empty)


def test_a_dry_run_does_not_care_about_the_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run writes nothing, so leftovers are not its problem.

    A gate that blocked the inspection path would be turned off, and then it
    would protect nothing.
    """
    script = _load_history_extraction_script()
    out_dir = PROJECT_ROOT / ".review-eval-local" / "templates"
    monkeypatch.setattr(script, "_require_empty_out_dir", _fail_if_called, raising=True)

    async def _no_templates(
        _url: str,
        *,
        interface_type: str | None,
        limit: int | None,
        since: datetime | None = None,
        descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
    ) -> tuple[list[TaskTemplate], list[tuple[str, str]]]:
        return [], []

    monkeypatch.setattr(script, "_collect_templates", _no_templates, raising=True)
    exit_code = script.main([
        "--database-url",
        "sqlite+aiosqlite:///unused.db",
        "--out-dir",
        str(out_dir.relative_to(PROJECT_ROOT)),
        "--dry-run",
    ])

    assert exit_code == 0


def _fail_if_called(_out_dir: Path) -> None:
    raise AssertionError("--dry-run must not check the output directory")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("2026-06-01", datetime(2026, 6, 1, tzinfo=UTC), id="bare-date"),
        pytest.param(
            "2026-06-01T12:30:00",
            datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
            id="naive-datetime-is-utc",
        ),
        pytest.param(
            "2026-06-01T12:30:00+00:00",
            datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
            id="utc-offset",
        ),
        pytest.param(
            # SQLite does not preserve the offset, so passing this through
            # would compare the wall clock 12:30 rather than the instant it
            # names — widening the window on one backend and not the other.
            "2026-06-01T12:30:00-07:00",
            datetime(2026, 6, 1, 19, 30, tzinfo=UTC),
            id="negative-offset-converted-to-the-instant",
        ),
        pytest.param(
            "2026-06-01T12:30:00+10:00",
            datetime(2026, 6, 1, 2, 30, tzinfo=UTC),
            id="positive-offset-converted-to-the-instant",
        ),
    ],
)
def test_since_resolves_every_form_to_an_instant_in_utc(
    raw: str, expected: datetime
) -> None:
    """`timestamp` is timezone-aware, so the bound is resolved before the driver.

    PostgreSQL errors on a naive comparison and SQLite compares silently wrong;
    SQLite also drops the offset, so an offset value has to arrive already
    converted rather than merely equal.

    Equality alone cannot check that: Python compares aware datetimes by
    instant, so ``12:30-07:00 == 19:30+00:00`` is true however the value was
    produced. The offset itself is the assertion that matters.
    """
    script = _load_history_extraction_script()
    resolved = script._utc_datetime(raw)

    assert resolved == expected
    assert resolved.utcoffset() == timedelta(0)
    assert resolved.replace(tzinfo=None) == expected.replace(tzinfo=None)


def test_since_rejects_something_that_is_not_a_date() -> None:
    script = _load_history_extraction_script()
    with pytest.raises(argparse.ArgumentTypeError, match="ISO 8601"):
        script._utc_datetime("last tuesday")


def test_since_defaults_to_reading_all_history() -> None:
    script = _load_history_extraction_script()
    assert (
        script._parse_args(["--database-url", "sqlite+aiosqlite:///x.db"]).since is None
    )


def test_the_extractor_abstracts_a_call_to_a_tool_only_a_snapshot_supplies(
    tmp_path: Path,
) -> None:
    """A registry snapshot is what makes MCP tool calls extractable at all.

    Without one the extractor resolves only tools compiled into this source
    tree, so a deployment's whole MCP surface -- transport, search, maps --
    was abstracted into templates that the chokepoint then refused for an
    unresolvable tool name.
    """
    script = _load_history_extraction_script()
    registry = snapshot_to_registry(
        descriptors_to_snapshot([
            ToolDescriptor(
                name="plan_trip",
                definition={
                    "type": "function",
                    "function": {
                        "name": "plan_trip",
                        "description": "Plan a public transport trip.",
                        "parameters": {
                            "type": "object",
                            "properties": {"origin": {"type": "string"}},
                        },
                    },
                },
                tags=frozenset({ToolTag.READ_ONLY}),
                origin="mcp",
                mcp_server_id="transport",
            )
        ])
    )
    rows = [
        cast(
            "MessageHistoryRow",
            {
                "role": "assistant",
                "tool_calls": [
                    ToolCallItem(
                        id="call-1",
                        type="function",
                        function=ToolCallFunction(
                            name="plan_trip",
                            arguments={"origin": "Central"},
                        ),
                    )
                ],
                "taint_metadata": _TRUSTED,
            },
        )
    ]
    grouped = {("telegram", "conv-1"): rows}

    committable, rejected = script._partition_templates(
        grouped, limit=None, descriptor_registry=registry
    )

    assert rejected == []
    assert [template.tool_names for template in committable] == [["plan_trip"]]
    assert committable[0].argument_shapes == {"origin": "string"}

    without_snapshot, refused = script._partition_templates(grouped, limit=None)
    assert without_snapshot == []
    assert "does not resolve in the registry" in refused[0][1]


def test_a_broken_tool_registry_stops_the_run_naming_the_file(tmp_path: Path) -> None:
    """A snapshot that cannot be read is not silently replaced by the local list.

    Falling back would run the whole extraction against a registry the
    maintainer did not ask for and report its rejections as history's shape.
    """
    script = _load_history_extraction_script()
    broken = tmp_path / "registry.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit, match="not valid JSON"):
        script._load_registry(str(broken))

    assert script._load_registry(None) is None


def _load_history_extraction_script() -> ModuleType:
    """Load the export script by path; ``scripts/`` is not an importable package."""
    script_path = PROJECT_ROOT / "scripts" / "extract_review_history.py"
    spec = importlib.util.spec_from_file_location(
        "extract_review_history_script", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
