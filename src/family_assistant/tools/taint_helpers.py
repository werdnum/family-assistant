"""Helpers for tool implementations that surface tainted stored artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from family_assistant.security.taint import (
    SensitiveReadScope,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_taint_state_into_tracker,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from family_assistant.tools.types import ToolExecutionContext


def record_sensitive_read(
    exec_context: ToolExecutionContext,
    *,
    kind: Literal["notes", "documents", "message_history", "attachments", "tool"],
    qualifier: str,
    surfaced_ids: Iterable[str],
    query_origin: Literal[
        "direct_user", "model_generated", "tool_or_history"
    ] = "model_generated",
) -> None:
    """Record that a tool surfaced a sensitive corpus scope this turn."""
    tracker = exec_context.taint_tracker
    if tracker is None:
        return
    scope = SensitiveReadScope(
        kind=kind,
        qualifier=qualifier,
        surfaced_ids=frozenset(surfaced_ids),
    )
    tracker.replace(
        tracker.snapshot().add_sensitive_read(scope, query_origin=query_origin)
    )


def tool_attachment_taint_state(exec_context: ToolExecutionContext) -> TurnTaintState:
    """The provenance a tool-stored attachment carries.

    The storing turn's taint merged with the executing call's own declared
    output provenance, so an attachment registered mid-call carries the tier its
    result will raise the turn to whether or not anything external came first.
    """
    state = (
        exec_context.taint_tracker.snapshot()
        if exec_context.taint_tracker is not None
        else TurnTaintState.empty()
    )
    if exec_context.in_flight_result_taint is not None:
        state = state.add_source(exec_context.in_flight_result_taint)
    return state


def merge_artifact_taint_into_context(
    exec_context: ToolExecutionContext,
    *,
    provenance_metadata: Mapping[str, object] | None,
    fallback_source_type: TaintSourceType,
    fallback_source_id: str | None,
    fallback_reason: str,
) -> None:
    """Merge stored artifact provenance into the current turn taint tracker."""
    tracker = exec_context.taint_tracker
    if tracker is None or provenance_metadata is None:
        return

    raw_taint_metadata = provenance_metadata.get("taint_metadata")
    state = TurnTaintState.from_metadata(raw_taint_metadata)
    if state.sources:
        merge_taint_state_into_tracker(tracker, state)
        return

    raw_tier = provenance_metadata.get("source_trust_tier")
    if raw_tier is None:
        return
    try:
        tier = SourceTrustTier.from_value(raw_tier)
    except ValueError:
        tier = SourceTrustTier.UNKNOWN_EXTERNAL
    raw_labels = provenance_metadata.get("provenance_labels")
    labels = (
        frozenset(str(label) for label in raw_labels)
        if isinstance(raw_labels, list)
        else frozenset()
    )
    tracker.add_source(
        TaintSource(
            source_type=fallback_source_type,
            source_id=fallback_source_id,
            tier=tier,
            labels=labels,
            reason=fallback_reason,
        )
    )


def inherit_attachment_taint(
    exec_context: ToolExecutionContext,
    *,
    attachment_metadata: Mapping[str, object] | None,
    attachment_id: str,
) -> None:
    """Raise the turn's taint by the stored provenance of an attachment read.

    For tools that return an attachment's own content and are therefore graded
    by the attachment rather than by a static output tag. An attachment stored
    without any provenance stamp is treated as unknown_external, so an unstamped
    registration path cannot read as trusted.
    """
    tracker = exec_context.taint_tracker
    if tracker is None:
        return
    if attachment_metadata is None or (
        "taint_metadata" not in attachment_metadata
        and "source_trust_tier" not in attachment_metadata
    ):
        tracker.add_source(
            TaintSource(
                source_type=TaintSourceType.ATTACHMENT,
                source_id=attachment_id,
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="Attachment has no stored provenance.",
            )
        )
        return
    merge_artifact_taint_into_context(
        exec_context,
        provenance_metadata=attachment_metadata,
        fallback_source_type=TaintSourceType.ATTACHMENT,
        fallback_source_id=attachment_id,
        fallback_reason="Attachment read provenance.",
    )
