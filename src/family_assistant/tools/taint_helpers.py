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
    kind: Literal["notes", "documents", "message_history", "attachments"],
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
