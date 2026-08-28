"""Repository for runtime taint audit events."""

from datetime import datetime

from sqlalchemy import func, insert, select

from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.taint_audit import taint_audit_events_table
from family_assistant.storage.types import (
    TaintAuditArgumentsSummary,
    TaintAuditEventRow,
    TaintAuditReviewContext,
    TaintAuditSourceSummary,
)


class TaintAuditEventsRepository(BaseRepository):
    """Repository for durable runtime taint audit events."""

    async def add(
        self,
        *,
        event_id: str,
        event_type: str,
        conversation_id: str,
        turn_id: str | None,
        processing_profile_id: str | None,
        subconversation_id: str | None,
        tool_name: str,
        tool_call_id: str | None,
        sink_class: str | None,
        max_tier: str,
        sources: list[TaintAuditSourceSummary],
        requested_outcome: str | None,
        effective_outcome: str | None,
        mode: str | None,
        reason: str,
        arguments_summary: TaintAuditArgumentsSummary | None,
        artifact_id: str | None = None,
        review_verdict: str | None = None,
        review_status: str | None = None,
        review_latency_ms: float | None = None,
        review_context: TaintAuditReviewContext | None = None,
    ) -> None:
        """Persist a taint audit event."""
        stmt = insert(taint_audit_events_table).values(
            event_id=event_id,
            event_type=event_type,
            conversation_id=conversation_id,
            turn_id=turn_id,
            processing_profile_id=processing_profile_id,
            subconversation_id=subconversation_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            sink_class=sink_class,
            max_tier=max_tier,
            sources_json=sources,
            requested_outcome=requested_outcome,
            effective_outcome=effective_outcome,
            mode=mode,
            review_verdict=review_verdict,
            review_status=review_status,
            review_latency_ms=review_latency_ms,
            review_context_json=review_context,
            reason=reason,
            arguments_summary_json=arguments_summary,
            artifact_id=artifact_id,
        )
        await self._execute_with_logging("add_taint_audit_event", stmt)

    async def list_for_turn(self, turn_id: str) -> list[TaintAuditEventRow]:
        """Return audit events for a processing turn in creation order."""
        stmt = (
            select(taint_audit_events_table)
            .where(taint_audit_events_table.c.turn_id == turn_id)
            .order_by(taint_audit_events_table.c.created_at.asc())
        )
        rows = await self._db.fetch_all(stmt)
        return rows  # type: ignore[return-value]  # rows match TaintAuditEventRow

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[TaintAuditEventRow]:
        """Return recent audit events for a conversation."""
        stmt = select(taint_audit_events_table).where(
            taint_audit_events_table.c.conversation_id == conversation_id
        )
        if since is not None:
            stmt = stmt.where(taint_audit_events_table.c.created_at >= since)
        stmt = stmt.order_by(taint_audit_events_table.c.created_at.desc()).limit(limit)
        rows = await self._db.fetch_all(stmt)
        return rows  # type: ignore[return-value]  # rows match TaintAuditEventRow

    async def count_since(self, since: datetime) -> int:
        """Return the number of audit events created at or after ``since``."""
        stmt = (
            select(func.count().label("count"))
            .select_from(taint_audit_events_table)
            .where(taint_audit_events_table.c.created_at >= since)
        )
        row = await self._db.fetch_one(stmt)
        return int(row["count"]) if row is not None else 0

    async def list_since(
        self,
        since: datetime,
        *,
        limit: int,
    ) -> list[TaintAuditEventRow]:
        """Return recent audit events in the diagnostics window."""
        stmt = (
            select(taint_audit_events_table)
            .where(taint_audit_events_table.c.created_at >= since)
            .order_by(taint_audit_events_table.c.created_at.desc())
            .limit(limit)
        )
        rows = await self._db.fetch_all(stmt)
        return rows  # type: ignore[return-value]  # rows match TaintAuditEventRow
