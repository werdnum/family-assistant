"""Unit tests for the live-capture path of the tool-call review harness."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import yaml

from family_assistant.config_models import (
    ToolCallReviewCaptureConfig,
    ToolCallReviewConfig,
)
from family_assistant.eval.tool_call_review.loader import load_cases
from family_assistant.eval.tool_call_review.schema import (
    ConversationPayload,
    resolve_tool_descriptor,
)
from family_assistant.llm.messages import dict_to_message
from family_assistant.security.taint import SinkClass, TurnTaintState
from family_assistant.services.tool_call_review import (
    DelegatingPolicyContext,
    ToolCallReviewConstraints,
    ToolCallReviewInput,
    ToolCallReviewVerdict,
)
from family_assistant.tools import infrastructure
from family_assistant.tools.infrastructure import (
    TaintTrackingToolsProvider,
    build_review_capture_case,
)

if TYPE_CHECKING:
    from pathlib import Path

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
    assert case.label == "benign"

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


async def test_serialize_and_write_produces_loadable_file(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    await infrastructure._serialize_and_write_review_capture(
        _make_review_input(),
        _full_constraints(),
        audit_event_id="evt-1",
        directory=str(directory),
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
