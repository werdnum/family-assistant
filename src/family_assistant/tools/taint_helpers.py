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

PUBLIC_BROWSER_SNAPSHOT_SCOPE_KEY = "public_browser_snapshot_scope"


def is_own_public_browser_snapshot(
    attachment_metadata: Mapping[str, object] | None,
    *,
    current_browser_scope: str | None,
) -> bool:
    """Whether an attachment carries the affirmative public-browser-snapshot stamp for this scope.

    Stamped at auto-conversion time only for unprotected, non-handed-back
    browser sessions. Historical attachments, user uploads, and snapshots from
    other scopes or protected/handed-back sessions lack a matching stamp and
    fall back to ordinary private-read accounting.
    """
    if not current_browser_scope or attachment_metadata is None:
        return False
    if attachment_metadata.get("large_result_auto_convert") is not True:
        return False
    snapshot_scope = attachment_metadata.get(PUBLIC_BROWSER_SNAPSHOT_SCOPE_KEY)
    return isinstance(snapshot_scope, str) and snapshot_scope == current_browser_scope


async def record_attachment_read(
    exec_context: ToolExecutionContext,
    *,
    attachment_id: str,
    attachment_metadata: Mapping[str, object] | None,
    qualifier: str,
) -> None:
    """Record private-read accounting for an attachment read, skipping own public browser snapshots."""
    from family_assistant.tools.browser_session import (  # noqa: PLC0415 - avoid cycle with browser_session
        browser_scope_key,
    )

    current_scope = await browser_scope_key(exec_context)
    if is_own_public_browser_snapshot(
        attachment_metadata,
        current_browser_scope=current_scope,
    ):
        exec_context.sensitive_read_exempted = True
        return
    record_sensitive_read(
        exec_context,
        kind="attachments",
        qualifier=qualifier,
        surfaced_ids=[attachment_id],
    )


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
    by the attachment rather than by a static output tag.
    """
    inherit_stored_artifact_taint(
        exec_context,
        provenance_metadata=attachment_metadata,
        source_type=TaintSourceType.ATTACHMENT,
        source_id=attachment_id,
        reason="Attachment read provenance.",
    )


def inherit_stored_artifact_taint(
    exec_context: ToolExecutionContext,
    *,
    provenance_metadata: Mapping[str, object] | None,
    source_type: TaintSourceType,
    source_id: str | None,
    reason: str,
) -> None:
    """Raise the turn's taint by a stored artifact's provenance stamp.

    An artifact stored without any provenance stamp is treated as
    unknown_external, so an unstamped write path cannot read as trusted.
    """
    tracker = exec_context.taint_tracker
    if tracker is None:
        return
    if provenance_metadata is None or (
        "taint_metadata" not in provenance_metadata
        and "source_trust_tier" not in provenance_metadata
    ):
        tracker.add_source(
            TaintSource(
                source_type=source_type,
                source_id=source_id,
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason=f"{reason} No stored provenance.",
            )
        )
        return
    merge_artifact_taint_into_context(
        exec_context,
        provenance_metadata=provenance_metadata,
        fallback_source_type=source_type,
        fallback_source_id=source_id,
        fallback_reason=reason,
    )
