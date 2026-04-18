"""Repository for email action proposal persistence."""

from __future__ import annotations

from sqlalchemy import insert, select

from family_assistant.storage.email_action_proposals import (
    EmailActionProposalData,
    email_action_proposals_table,
)
from family_assistant.storage.repositories.base import BaseRepository


class EmailActionProposalsRepository(BaseRepository):
    """Repository for pending actions proposed from inbound email."""

    async def add_many(self, proposals: list[EmailActionProposalData]) -> list[int]:
        """Store email action proposals and return their database IDs."""
        if not proposals:
            return []

        values = [proposal.model_dump() for proposal in proposals]
        stmt = (
            insert(email_action_proposals_table)
            .values(values)
            .returning(email_action_proposals_table.c.id)
        )
        result = await self._db.execute_with_retry(stmt)
        return [int(row[0]) for row in result.fetchall()]

    async def list_for_email(self, email_id: int) -> list[dict[str, object]]:
        """Return proposals for one stored email."""
        stmt = (
            select(email_action_proposals_table)
            .where(email_action_proposals_table.c.email_id == email_id)
            .order_by(email_action_proposals_table.c.id.asc())
        )
        rows = await self._db.fetch_all(stmt)
        return [dict(row) for row in rows]

    async def list_for_user(
        self, target_user_id: str, status: str = "proposed", limit: int = 50
    ) -> list[dict[str, object]]:
        """Return recent proposals for a target user."""
        stmt = (
            select(email_action_proposals_table)
            .where(email_action_proposals_table.c.target_user_id == target_user_id)
            .where(email_action_proposals_table.c.status == status)
            .order_by(email_action_proposals_table.c.created_at.desc())
            .limit(limit)
        )
        rows = await self._db.fetch_all(stmt)
        return [dict(row) for row in rows]
