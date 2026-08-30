"""Unit tests for the public-corpus adapters.

These import the adapter subpackage and the harness loader/schema by full path
rather than through the package ``__init__`` (which pulls in the runner), so the
mapping is exercised without a configured judge.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from family_assistant.eval.tool_call_review.adapters import (
    ADAPTERS,
    DeepsetPromptInjectionsAdapter,
    InjecAgentAdapter,
    lineage_aware_dedup,
)
from family_assistant.eval.tool_call_review.adapters.base import (
    AdaptedCase,
    Adapter,
    normalized_text_key,
)
from family_assistant.eval.tool_call_review.loader import (
    load_cases,
    validate_against_tool_schema,
)
from family_assistant.eval.tool_call_review.schema import ConversationPayload
from family_assistant.services.tool_call_review import ToolCallReviewInput

if TYPE_CHECKING:
    from pathlib import Path

_ADAPTER_CLASSES = (DeepsetPromptInjectionsAdapter, InjecAgentAdapter)


@pytest.mark.parametrize("adapter_cls", _ADAPTER_CLASSES)
def test_adapter_is_registered(adapter_cls: type[Adapter]) -> None:
    """Each adapter is discoverable by its corpus id."""
    assert ADAPTERS[adapter_cls.corpus_id] is adapter_cls


@pytest.mark.parametrize("adapter_cls", _ADAPTER_CLASSES)
def test_sample_maps_to_valid_cases(adapter_cls: type[Adapter]) -> None:
    """Sample rows map into schema-valid cases with both labels present."""
    adapter = adapter_cls.from_sample()
    adapted = list(adapter.iter_adapted())
    assert adapted, "adapter produced no cases from its sample"
    labels = {item.case.label for item in adapted}
    assert labels == {"attack", "benign"}, (
        "adapter must produce benign twins alongside attacks"
    )
    for item in adapted:
        case = item.case
        assert case.boundary == "conversation"
        assert case.source == f"public:{adapter_cls.corpus_id}"
        assert case.attack_class is not None
        # Arguments must satisfy the live tool schema, or the case would replay
        # a now-impossible call that still counts as a clean trial.
        validate_against_tool_schema(case)


@pytest.mark.parametrize("adapter_cls", _ADAPTER_CLASSES)
def test_cases_load_via_loader_and_round_trip(
    adapter_cls: type[Adapter], tmp_path: Path
) -> None:
    """Written cases load through the loader and rebuild a reviewer input."""
    adapter = adapter_cls.from_sample()
    cases_file = tmp_path / f"{adapter_cls.corpus_id}.jsonl"
    with cases_file.open("w", encoding="utf-8") as handle:
        for case in adapter.iter_cases():
            handle.write(json.dumps(case.model_dump(mode="json"), ensure_ascii=False))
            handle.write("\n")

    loaded = load_cases(cases_file)
    assert len(loaded) == len(list(adapter.iter_cases()))
    for case in loaded:
        assert isinstance(case.payload, ConversationPayload)
        review_input, constraints = case.to_review_input()
        assert isinstance(review_input, ToolCallReviewInput)
        # Fallback must be a real, in-space, non-allow verdict.
        assert constraints.fallback_verdict.value != "allow"
        assert constraints.fallback_verdict in constraints.available_verdicts


@pytest.mark.parametrize("adapter_cls", _ADAPTER_CLASSES)
def test_injection_paired_with_gated_call_under_taint(
    adapter_cls: type[Adapter],
) -> None:
    """Attack cases carry the injection in a non-trusted row and a gated call."""
    adapter = adapter_cls.from_sample()
    attacks = [item for item in adapter.iter_adapted() if item.case.label == "attack"]
    assert attacks, "adapter produced no attack cases"
    for item in attacks:
        review_input, _ = item.case.to_review_input()
        assert isinstance(review_input, ToolCallReviewInput)
        # The turn taint must be untrusted: a trusted turn would not reach the
        # reviewer at all, so a case that lost its provenance is a broken case.
        assert review_input.taint_state.max_tier.config_value != "trusted_user"
        # At least one message row must carry untrusted taint (the injection).
        untrusted_rows = [
            message
            for message in review_input.messages
            if getattr(message, "taint_metadata", None) is not None
        ]
        assert untrusted_rows, "injection must sit in a taint-bearing row"
        # The gated call must carry arguments (the action the injection argues
        # for), not be a bare text record.
        assert review_input.arguments


@pytest.mark.parametrize("adapter_cls", _ADAPTER_CLASSES)
def test_lineage_fields_populated(adapter_cls: type[Adapter]) -> None:
    """Every adapted case carries a fully-populated lineage record."""
    adapter = adapter_cls.from_sample()
    for item in adapter.iter_adapted():
        lineage = item.lineage
        assert lineage.corpus_id == adapter_cls.corpus_id
        assert lineage.upstream_id
        assert lineage.group
        assert lineage.license == adapter_cls.license
        assert lineage.text_key
        # Dedup identity is the normalized text alone, independent of group.
        assert lineage.dedup_key == lineage.text_key


def test_lineage_aware_dedup_collapses_repeats() -> None:
    """A repeated lineage key is dropped, keeping the first occurrence."""
    adapter = InjecAgentAdapter.from_sample()
    adapted = list(adapter.iter_adapted())
    duplicated = [*adapted, *adapted]
    deduped = lineage_aware_dedup(duplicated)
    assert len(deduped) == len(adapted)
    assert [item.case.id for item in deduped] == [item.case.id for item in adapted]


def test_lineage_aware_dedup_collapses_identical_text_across_groups() -> None:
    """The same injection text under different corpus groups deduplicates to one.

    Group is corpus-specific (``deepset:…`` vs ``injecagent:…``), so keeping it
    in the dedup identity would let the same attack straddle a dev/gate split.
    """
    adapter = InjecAgentAdapter.from_sample()
    original = next(iter(adapter.iter_adapted()))
    twin_key = original.lineage.text_key
    same_text_other_corpus = AdaptedCase(
        case=original.case,
        lineage=replace(
            original.lineage,
            corpus_id="deepset_prompt_injections",
            group=f"deepset:{twin_key[:16]}",
        ),
    )
    assert same_text_other_corpus.lineage.group != original.lineage.group
    deduped = lineage_aware_dedup([original, same_text_other_corpus])
    assert len(deduped) == 1
    assert deduped[0] is original


def test_normalized_text_key_folds_whitespace_and_case() -> None:
    """The dedup key is stable across whitespace and case variation."""
    assert normalized_text_key("Ignore   ALL previous") == normalized_text_key(
        "ignore all previous"
    )
    assert normalized_text_key("a") != normalized_text_key("b")


def test_case_ids_are_unique_within_an_adapter() -> None:
    """Attack and benign twin ids never collide."""
    for adapter_cls in _ADAPTER_CLASSES:
        adapter = adapter_cls.from_sample()
        ids = [item.case.id for item in adapter.iter_adapted()]
        assert len(ids) == len(set(ids))


def test_deepset_attack_and_benign_split_matches_labels() -> None:
    """deepset label 1 rows become attacks; label 0 rows become benign twins."""
    adapter = DeepsetPromptInjectionsAdapter.from_sample()
    adapted = list(adapter.iter_adapted())
    attacks = sum(1 for item in adapted if item.case.label == "attack")
    benign = sum(1 for item in adapted if item.case.label == "benign")
    assert attacks == 3
    assert benign == 3


def test_from_path_reads_sample_csv() -> None:
    """The deepset adapter parses its committed CSV sample from a path."""
    sample = (
        DeepsetPromptInjectionsAdapter.sample_dir() / "prompt_injections_sample.csv"
    )
    adapter = DeepsetPromptInjectionsAdapter.from_path(sample)
    cases = list(adapter.iter_cases())
    assert len(cases) == 6


def test_adapted_case_dataclass_pairs_case_and_lineage() -> None:
    """iter_adapted yields AdaptedCase pairs, iter_cases yields the cases."""
    adapter = InjecAgentAdapter.from_sample()
    adapted = list(adapter.iter_adapted())
    assert all(isinstance(item, AdaptedCase) for item in adapted)
    assert [item.case for item in adapted] == list(
        InjecAgentAdapter.from_sample().iter_cases()
    )
