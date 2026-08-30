"""Unit tests for the offline history-extraction path of the review-eval harness."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.eval import private_paths
from family_assistant.eval.private_paths import (
    PrivateEvalPathError,
    resolve_private_eval_path,
)
from family_assistant.eval.tool_call_review.scrub import TaskTemplate
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.paths import PROJECT_ROOT
from family_assistant.storage.types import MessageHistoryRow

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from family_assistant.security.taint import TaintMetadata

pytestmark = pytest.mark.no_db

_TRUSTED = {
    "version": "runtime_v1",
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
