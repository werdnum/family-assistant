"""Structural checks for the hand-authored manual seed dataset.

These load the committed manual cases through the real loader and assert the
invariants the eval harness relies on: every attack class is represented, every
attack case has at least one benign twin sharing its ``attack_class``, the
genuine-ambiguity fixtures declare ``confirm``, and every conversation case
rebuilds a typed reviewer input via ``to_review_input()``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from family_assistant.eval.tool_call_review.loader import load_cases
from family_assistant.eval.tool_call_review.schema import (
    ConversationPayload,
    EvalCase,
)
from family_assistant.services.tool_call_review import (
    ToolCallReviewConstraints,
    ToolCallReviewInput,
)

_EIGHT_ATTACK_CLASSES = frozenset({
    "tool_result_injection",
    "email_intake_injection",
    "artifact_laundering",
    "delegation_goal_smuggling",
    "argument_lobbying",
    "boundary_forging",
    "exfiltration_shaping",
    "unattended_context",
})

_EXPECTED_CASE_COUNT = 53


def _manual_dataset_dir() -> Path:
    """Locate the committed manual dataset directory by package path."""
    spec = importlib.util.find_spec("family_assistant.eval.tool_call_review")
    assert spec is not None and spec.origin is not None
    dataset_dir = Path(spec.origin).parent / "datasets" / "manual"
    assert dataset_dir.is_dir(), f"missing manual dataset dir: {dataset_dir}"
    return dataset_dir


@pytest.fixture(scope="module")
def manual_cases() -> list[EvalCase]:
    return load_cases(_manual_dataset_dir())


def test_all_manual_cases_load(manual_cases: list[EvalCase]) -> None:
    assert len(manual_cases) == _EXPECTED_CASE_COUNT
    ids = [case.id for case in manual_cases]
    assert len(ids) == len(set(ids)), "manual case ids must be unique"
    assert all(case.source == "manual" for case in manual_cases)
    assert all(case.boundary == "conversation" for case in manual_cases)


def test_all_eight_attack_classes_present(manual_cases: list[EvalCase]) -> None:
    attack_classes = {
        case.attack_class for case in manual_cases if case.label == "attack"
    }
    assert attack_classes == _EIGHT_ATTACK_CLASSES


def test_every_attack_case_has_benign_twin(manual_cases: list[EvalCase]) -> None:
    benign_classes = {
        case.attack_class
        for case in manual_cases
        if case.label == "benign" and case.attack_class is not None
    }
    attack_cases = [case for case in manual_cases if case.label == "attack"]
    assert attack_cases, "dataset must contain attack cases"
    for case in attack_cases:
        assert case.attack_class is not None
        assert case.attack_class in benign_classes, (
            f"attack case {case.id!r} has no benign twin sharing "
            f"attack_class {case.attack_class!r}"
        )


def test_attack_cases_carry_attack_class(manual_cases: list[EvalCase]) -> None:
    for case in manual_cases:
        if case.label == "attack":
            assert case.attack_class is not None


def test_ambiguity_fixtures_expect_confirm(manual_cases: list[EvalCase]) -> None:
    ambiguity = [case for case in manual_cases if case.id.startswith("manual-ambig-")]
    assert len(ambiguity) == 2
    for case in ambiguity:
        assert case.label == "benign"
        assert case.expected_verdict == "confirm"
        assert case.expected_verdict in case.constraints.available_verdicts


def test_trusted_inlet_pin_expects_deny(manual_cases: list[EvalCase]) -> None:
    pin_cases = [
        case for case in manual_cases if case.id == "manual-trji-attack-trusted-inlet"
    ]
    assert len(pin_cases) == 1
    pin_case = pin_cases[0]
    assert pin_case.label == "attack"
    assert pin_case.expected_verdict == "deny"
    assert pin_case.expected_verdict in pin_case.constraints.available_verdicts


def test_declared_expected_verdicts_are_valid(manual_cases: list[EvalCase]) -> None:
    for case in manual_cases:
        if case.expected_verdict is not None:
            assert case.expected_verdict in case.constraints.available_verdicts


def test_fallback_verdicts_are_valid(manual_cases: list[EvalCase]) -> None:
    for case in manual_cases:
        fallback = case.constraints.fallback_verdict
        assert fallback != "allow"
        assert fallback in case.constraints.available_verdicts


def test_conversation_cases_round_trip(manual_cases: list[EvalCase]) -> None:
    conversation_cases = [
        case for case in manual_cases if isinstance(case.payload, ConversationPayload)
    ]
    assert len(conversation_cases) == _EXPECTED_CASE_COUNT
    for case in conversation_cases:
        payload = case.payload
        assert isinstance(payload, ConversationPayload)
        review_input, constraints = case.to_review_input()
        assert isinstance(review_input, ToolCallReviewInput)
        assert isinstance(constraints, ToolCallReviewConstraints)
        assert review_input.descriptor.name == payload.tool_name
        assert str(review_input.sink_class) == payload.sink_class
        assert len(review_input.messages) == len(payload.messages)
