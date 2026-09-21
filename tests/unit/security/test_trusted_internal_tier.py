"""Tests for the trusted-pole authorship split (``trusted_internal``)."""

from __future__ import annotations

import pytest

from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.security.taint import (
    TAINT_METADATA_VERSION,
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyEvaluator,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    is_externally_authored,
    is_human_direct_metadata,
    machine_authored_taint_metadata,
    merge_taint_policy_config,
)


def _tainted_state(tier: SourceTrustTier) -> TurnTaintState:
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-1",
            tier=tier,
            labels=frozenset(),
            reason="test",
        )
    )


def test_trusted_internal_ranks_between_trusted_user_and_known_contact() -> None:
    assert (
        SourceTrustTier.TRUSTED_USER
        < SourceTrustTier.TRUSTED_INTERNAL
        < SourceTrustTier.KNOWN_CONTACT
    )


def test_trusted_internal_round_trips_through_metadata() -> None:
    state = TurnTaintState.empty().with_authorship_floor()
    metadata = state.to_metadata()

    assert metadata.get("max_tier") == "trusted_internal"
    assert (
        TurnTaintState.from_metadata(metadata).max_tier
        is SourceTrustTier.TRUSTED_INTERNAL
    )
    assert SourceTrustTier.from_value("trusted_internal") is (
        SourceTrustTier.TRUSTED_INTERNAL
    )


@pytest.mark.parametrize(
    ("tier", "external"),
    [
        (SourceTrustTier.TRUSTED_USER, False),
        (SourceTrustTier.TRUSTED_INTERNAL, False),
        (SourceTrustTier.KNOWN_CONTACT, True),
        (SourceTrustTier.RECOGNIZED_MACHINE, True),
        (SourceTrustTier.UNKNOWN_EXTERNAL, True),
        (None, True),
    ],
)
def test_externally_authored_boundary(
    tier: SourceTrustTier | None,
    external: bool,
) -> None:
    assert is_externally_authored(tier) is external


def test_authorship_floor_only_ever_raises() -> None:
    assert (
        TurnTaintState.empty().with_authorship_floor().max_tier
        is SourceTrustTier.TRUSTED_INTERNAL
    )
    tainted = _tainted_state(SourceTrustTier.UNKNOWN_EXTERNAL)
    assert tainted.with_authorship_floor() is tainted


def test_machine_authored_rows_stamp_trusted_internal_by_construction() -> None:
    clean = TurnTaintState.empty().to_metadata()
    assert clean.get("max_tier") == "trusted_user"

    assistant = AssistantMessage(content="hi", taint_metadata=clean)
    tool = ToolMessage(tool_call_id="1", content="ok", name="t", taint_metadata=clean)

    for message in (assistant, tool):
        assert message.taint_metadata is not None
        assert message.taint_metadata.get("max_tier") == "trusted_internal"


def test_machine_authored_rows_keep_a_tainted_turns_stamp() -> None:
    tainted = _tainted_state(SourceTrustTier.UNKNOWN_EXTERNAL).to_metadata()

    assistant = AssistantMessage(content="hi", taint_metadata=tainted)

    assert assistant.taint_metadata is not None
    assert assistant.taint_metadata.get("max_tier") == "unknown_external"


def test_authorship_floor_never_fabricates_a_stamp() -> None:
    assert AssistantMessage(content="hi").taint_metadata is None


def test_human_typed_user_rows_keep_trusted_user() -> None:
    message = UserMessage(
        content="send it", taint_metadata=TurnTaintState.empty().to_metadata()
    )

    assert message.taint_metadata is not None
    assert message.taint_metadata.get("max_tier") == "trusted_user"
    assert is_human_direct_metadata(message.taint_metadata)


def test_machine_authored_metadata_is_not_human_direct() -> None:
    metadata = machine_authored_taint_metadata(TurnTaintState.empty())

    assert not is_human_direct_metadata(metadata)


def test_pre_split_trusted_user_rows_are_not_human_direct() -> None:
    """The epoch guard: ``runtime_v1`` conflated human and system authorship."""
    legacy = {**TurnTaintState.empty().to_metadata(), "version": "runtime_v1"}

    assert legacy.get("max_tier") == "trusted_user"
    assert not is_human_direct_metadata(legacy)
    assert is_human_direct_metadata({
        **legacy,
        "version": TAINT_METADATA_VERSION,
    })


def test_no_shipped_matrix_cell_distinguishes_the_trusted_tiers() -> None:
    evaluator = TaintPolicyEvaluator(TaintPolicyConfig())

    for sink_class in SinkClass:
        trusted_user = evaluator.evaluate(
            state=TurnTaintState.empty(),
            sink_class=sink_class,
        )
        trusted_internal = evaluator.evaluate(
            state=TurnTaintState.empty().with_authorship_floor(),
            sink_class=sink_class,
        )
        assert trusted_user.requested_outcome == trusted_internal.requested_outcome, (
            sink_class
        )


def test_operator_minimum_written_for_trusted_user_governs_trusted_internal() -> None:
    evaluator = TaintPolicyEvaluator(
        TaintPolicyConfig(
            operator_minimum={
                SourceTrustTier.TRUSTED_USER: {
                    SinkClass.ARTIFACT_WRITE: TaintPolicyOutcome.CONFIRM
                }
            }
        )
    )

    evaluation = evaluator.evaluate(
        state=TurnTaintState.empty().with_authorship_floor(),
        sink_class=SinkClass.ARTIFACT_WRITE,
    )

    assert evaluation.requested_outcome is TaintPolicyOutcome.CONFIRM


def test_matrix_override_written_for_trusted_user_governs_trusted_internal() -> None:
    evaluator = TaintPolicyEvaluator(
        TaintPolicyConfig(
            matrix_overrides={
                SourceTrustTier.TRUSTED_USER: {
                    SinkClass.HOME_LOCAL: TaintPolicyOutcome.DENY
                }
            }
        )
    )

    evaluation = evaluator.evaluate(
        state=TurnTaintState.empty().with_authorship_floor(),
        sink_class=SinkClass.HOME_LOCAL,
    )

    assert evaluation.requested_outcome is TaintPolicyOutcome.DENY


def test_explicit_trusted_internal_entry_overrides_the_inherited_one() -> None:
    evaluator = TaintPolicyEvaluator(
        TaintPolicyConfig(
            matrix_overrides={
                SourceTrustTier.TRUSTED_USER: {
                    SinkClass.HOME_LOCAL: TaintPolicyOutcome.DENY
                },
                SourceTrustTier.TRUSTED_INTERNAL: {
                    SinkClass.HOME_LOCAL: TaintPolicyOutcome.AUDIT
                },
            }
        )
    )

    evaluation = evaluator.evaluate(
        state=TurnTaintState.empty().with_authorship_floor(),
        sink_class=SinkClass.HOME_LOCAL,
    )

    assert evaluation.requested_outcome is TaintPolicyOutcome.AUDIT


def test_profile_cannot_relax_an_inherited_trusted_user_floor() -> None:
    base = TaintPolicyConfig(
        operator_minimum={
            SourceTrustTier.TRUSTED_USER: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.DENY
            }
        }
    )
    profile = TaintPolicyConfig(
        matrix_overrides={
            SourceTrustTier.TRUSTED_INTERNAL: {
                SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.ALLOW
            }
        }
    )

    with pytest.raises(ValueError, match="cannot relax base policy"):
        merge_taint_policy_config(base=base, profile=profile)
