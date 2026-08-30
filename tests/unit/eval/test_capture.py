"""Unit tests for the live-capture path of the tool-call review harness."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
import yaml

from family_assistant import config_models
from family_assistant.config_models import (
    PrivateEvalPathError,
    ToolCallReviewCaptureConfig,
    ToolCallReviewConfig,
    resolve_private_eval_path,
)
from family_assistant.eval.tool_call_review.loader import load_cases
from family_assistant.eval.tool_call_review.schema import (
    ConversationPayload,
    resolve_tool_descriptor,
)
from family_assistant.eval.tool_call_review.scrub import TaskTemplate
from family_assistant.llm.messages import dict_to_message
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.paths import PROJECT_ROOT
from family_assistant.security.taint import SinkClass, TurnTaintState
from family_assistant.services.tool_call_review import (
    DelegatingPolicyContext,
    ToolCallReviewConstraints,
    ToolCallReviewInput,
    ToolCallReviewVerdict,
)
from family_assistant.storage.types import MessageHistoryRow
from family_assistant.tools import infrastructure
from family_assistant.tools.infrastructure import (
    TaintTrackingToolsProvider,
    build_review_capture_case,
)

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


def _make_review_input() -> ToolCallReviewInput:
    descriptor = resolve_tool_descriptor("send_message_to_user")
    messages = [
        dict_to_message({
            "role": "user",
            "content": "Tell the family the plumber is confirmed.",
            "taint_metadata": _TRUSTED,
        })
    ]
    return ToolCallReviewInput(
        messages=messages,
        descriptor=descriptor,
        arguments={
            "target_chat_id": "1001",
            "message_content": "The plumber is confirmed for Tuesday.",
        },
        sink_class=SinkClass.KNOWN_USER_MESSAGE,
        taint_state=TurnTaintState.from_metadata(_TRUSTED),
        policy_contexts=[
            DelegatingPolicyContext(
                kind="taint_cell",
                identifier="known_contact.known_user_message",
                description="Outbound message to a configured household member.",
            )
        ],
        deployment_guidance="Relaying an asked-for update to a known member is fine.",
        profile_guidance="",
        trigger=None,
        destination_echo=None,
    )


def _full_constraints() -> ToolCallReviewConstraints:
    return ToolCallReviewConstraints(
        available_verdicts=frozenset(ToolCallReviewVerdict),
        fallback_verdict=ToolCallReviewVerdict.CONFIRM,
    )


def _external_capture_config(
    directory: Path, *, enabled: bool = True
) -> ToolCallReviewCaptureConfig:
    """Capture into a tmp directory, which is outside the repository tree.

    Containment is validated against ``<repo root>/.review-eval-local/``, so a
    test exercising the *write* path must opt out of it explicitly rather than
    dressing a tmp path up with the marker name.
    """
    return ToolCallReviewCaptureConfig(
        enabled=enabled,
        directory=str(directory),
        allow_external_directory=True,
    )


class _CaptureHarness:
    """Minimal stand-in exercising the real capture methods off the wrapper.

    ``_start_review_capture`` reads only ``_review_config`` and ``_review_tasks``
    and schedules a detached task, so binding the real methods onto this stub
    exercises the shipped gate and spawn logic without constructing the whole
    provider.
    """

    _start_review_capture = TaintTrackingToolsProvider._start_review_capture
    _finish_review_capture = TaintTrackingToolsProvider._finish_review_capture

    def __init__(self, config: ToolCallReviewConfig) -> None:
        self._review_config = config
        self._review_tasks: set[asyncio.Task[object]] = set()


async def _drain(harness: _CaptureHarness) -> None:
    tasks = list(harness._review_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def test_build_capture_case_serializes_a_loadable_case(tmp_path: Path) -> None:
    case = build_review_capture_case(
        _make_review_input(), _full_constraints(), audit_event_id="evt-abc"
    )
    assert case.id == "live-capture-evt-abc"
    assert case.source == "live_capture"
    assert case.label == "unlabeled"

    path = tmp_path / "capture.yaml"
    path.write_text(
        yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    loaded = load_cases(path)
    assert len(loaded) == 1
    assert loaded[0].id == "live-capture-evt-abc"
    payload = loaded[0].payload
    assert isinstance(payload, ConversationPayload)
    assert payload.tool_name == "send_message_to_user"


def test_capture_case_is_unlabeled_so_it_scores_nothing(tmp_path: Path) -> None:
    """A capture has no ground truth, and the label says so rather than guessing.

    Written as ``unlabeled``, the replayed capture lands in the report's
    observational slice; written as ``benign`` it would report a correct deny
    of an injected capture as friction.
    """
    case = build_review_capture_case(
        _make_review_input(), _full_constraints(), audit_event_id="evt-unlabeled"
    )
    assert case.label == "unlabeled"
    assert case.attack_class is None
    assert case.expected_verdict is None

    path = tmp_path / "capture.yaml"
    path.write_text(
        yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    assert [loaded.label for loaded in load_cases(path)] == ["unlabeled"]


async def test_serialize_and_write_produces_loadable_file(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    await infrastructure._serialize_and_write_review_capture(
        _make_review_input(),
        _full_constraints(),
        audit_event_id="evt-1",
        directory=directory,
    )
    loaded = load_cases(directory)
    assert [case.id for case in loaded] == ["live-capture-evt-1"]
    assert loaded[0].source == "live_capture"


async def test_capture_enabled_writes_file(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    config = ToolCallReviewConfig(capture=_external_capture_config(directory))
    harness = _CaptureHarness(config)

    harness._start_review_capture(
        review_input=_make_review_input(),
        constraints=_full_constraints(),
        audit_event_id="evt-2",
    )
    assert harness._review_tasks, "an enabled capture should schedule a task"
    await _drain(harness)

    loaded = load_cases(directory)
    assert [case.id for case in loaded] == ["live-capture-evt-2"]


async def test_capture_disabled_writes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    config = ToolCallReviewConfig(
        capture=_external_capture_config(directory, enabled=False)
    )
    harness = _CaptureHarness(config)

    result = harness._start_review_capture(
        review_input=_make_review_input(),
        constraints=_full_constraints(),
        audit_event_id="evt-3",
    )
    assert result is None
    assert harness._review_tasks == set()
    assert not directory.exists()


async def test_capture_failure_never_breaks_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "captures"

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("serialization exploded")

    monkeypatch.setattr(infrastructure, "build_review_capture_case", _boom)

    config = ToolCallReviewConfig(capture=_external_capture_config(directory))
    harness = _CaptureHarness(config)

    # The scheduling call must return normally even though the write will fail.
    harness._start_review_capture(
        review_input=_make_review_input(),
        constraints=_full_constraints(),
        audit_event_id="evt-4",
    )
    # Draining the swallowing task must not raise the write's exception.
    await _drain(harness)
    assert not any(directory.glob("*.yaml")) if directory.exists() else True


def test_shipped_default_capture_is_disabled() -> None:
    assert ToolCallReviewConfig().capture.enabled is False
    assert ToolCallReviewConfig().capture.directory == ".review-eval-local/captures"


@pytest.mark.parametrize(
    "directory",
    [
        pytest.param("captures", id="no-marker"),
        # Names the marker but resolves out of it again.
        pytest.param(".review-eval-local/../captures", id="traversal"),
        # Carries the marker name, but `.gitignore` ignores only the
        # repository-root `.review-eval-local/`, so this lands in a *tracked*
        # directory and would commit raw household content.
        pytest.param("nested/.review-eval-local/captures", id="nested-marker"),
    ],
)
def test_capture_directory_outside_private_tree_is_rejected(directory: str) -> None:
    with pytest.raises(ValueError, match=".review-eval-local"):
        ToolCallReviewCaptureConfig(enabled=True, directory=directory)


def test_capture_directory_resolves_against_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative directory anchors at the repository root, not the cwd.

    Containment is validated against ``<repo root>/.review-eval-local/``, so a
    writer that resolved the raw string itself would send raw household content
    somewhere validation never looked whenever the process runs from elsewhere.
    """
    monkeypatch.chdir(tmp_path)
    config = ToolCallReviewCaptureConfig(enabled=True)
    assert config.resolved_directory == PROJECT_ROOT / ".review-eval-local" / "captures"
    assert not config.resolved_directory.is_relative_to(tmp_path)


async def test_relative_capture_directory_writes_under_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed service started from another cwd still captures privately."""
    fake_root = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(config_models, "PROJECT_ROOT", fake_root)
    monkeypatch.chdir(elsewhere)

    config = ToolCallReviewConfig(
        capture=ToolCallReviewCaptureConfig(
            enabled=True, directory=".review-eval-local/captures"
        )
    )
    harness = _CaptureHarness(config)
    harness._start_review_capture(
        review_input=_make_review_input(),
        constraints=_full_constraints(),
        audit_event_id="evt-cwd",
    )
    await _drain(harness)

    private_dir = fake_root / ".review-eval-local" / "captures"
    assert [case.id for case in load_cases(private_dir)] == ["live-capture-evt-cwd"]
    assert not (elsewhere / ".review-eval-local").exists()


def test_capture_directory_inside_repository_private_tree_is_accepted() -> None:
    config = ToolCallReviewCaptureConfig(
        enabled=True, directory=".review-eval-local/captures/friction"
    )
    assert config.directory == ".review-eval-local/captures/friction"


@pytest.mark.parametrize(
    "directory",
    [
        pytest.param("/mnt/private/captures", id="absolute"),
        pytest.param("nested/.review-eval-local/captures", id="nested-marker"),
    ],
)
def test_capture_directory_override_allows_external_path(directory: str) -> None:
    # An explicit opt-in permits capturing to a deliberately private location
    # outside the gitignored tree (e.g. a mounted volume).
    config = ToolCallReviewCaptureConfig(
        enabled=True,
        directory=directory,
        allow_external_directory=True,
    )
    assert config.directory == directory


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
    monkeypatch.setattr(config_models, "PROJECT_ROOT", tmp_path)
    tracked = tmp_path / "src" / "datasets"
    tracked.mkdir(parents=True)
    private_root = tmp_path / ".review-eval-local"
    private_root.mkdir()
    (private_root / "captures").symlink_to(tracked)

    with pytest.raises(PrivateEvalPathError, match=".review-eval-local"):
        resolve_private_eval_path(".review-eval-local/captures")


def test_private_eval_path_rejects_a_symlinked_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked marker makes containment vacuous, not merely inaccurate.

    Candidate and root both resolve under the link's target, so every write
    passes the check on its way into a tracked directory.
    """
    monkeypatch.setattr(config_models, "PROJECT_ROOT", tmp_path)
    tracked = tmp_path / "src" / "datasets"
    tracked.mkdir(parents=True)
    (tmp_path / ".review-eval-local").symlink_to(tracked)

    with pytest.raises(PrivateEvalPathError, match="is a symlink"):
        resolve_private_eval_path(".review-eval-local/captures")


def test_private_eval_path_accepts_a_real_path_under_the_private_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_models, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".review-eval-local" / "captures").mkdir(parents=True)

    resolved = resolve_private_eval_path(".review-eval-local/captures/today.jsonl")

    assert resolved == tmp_path / ".review-eval-local" / "captures" / "today.jsonl"


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
    monkeypatch.setattr(config_models, "PROJECT_ROOT", linked_root)

    assert (
        resolve_private_eval_path(".review-eval-local/captures")
        == real_root / ".review-eval-local" / "captures"
    )
    assert (
        resolve_private_eval_path(linked_root / ".review-eval-local" / "captures")
        == real_root / ".review-eval-local" / "captures"
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
    """The template export refuses the same paths a capture directory refuses.

    The finding this covers is that a marker-name check accepted
    ``nested/.review-eval-local/templates``, which `.gitignore` tracks; both
    writers now resolve through one rule, so neither can drift from it.
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
