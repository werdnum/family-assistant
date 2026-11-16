"""Repository for stored Claude Code sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, insert, select
from sqlalchemy.sql import functions as func

from family_assistant.storage.claude_agent_sessions import (
    claude_agent_session_entries_table,
    claude_agent_session_turns_table,
)
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import DatabaseTransaction

_entries = claude_agent_session_entries_table
_turns = claude_agent_session_turns_table


@dataclass(frozen=True)
class ClaudeAgentSessionTurn:
    """A turn a stored session ran in full."""

    session_id: str
    turn_id: str
    model: str
    effort: str | None
    system_prompt_sha256: str
    taint_metadata_json: TaintMetadata | None


class ClaudeAgentSessionsRepository(BaseRepository):
    """The Agent SDK session store's rows, and the turns sessions ran."""

    async def append_entries(
        self, *, project_key: str, session_id: str, subpath: str, entries: list[str]
    ) -> None:
        """Append serialised transcript entries, in order, in one transaction."""
        now = datetime.now(UTC)
        if not entries:
            return
        await self._db.execute(
            insert(_entries).values([
                {
                    "project_key": project_key,
                    "session_id": session_id,
                    "subpath": subpath,
                    "entry": entry,
                    "created_at": now,
                }
                for entry in entries
            ])
        )

    async def load_entries(
        self, *, project_key: str, session_id: str, subpath: str
    ) -> list[str]:
        """Every entry appended under the key, in append order."""
        rows = await self._db.fetch_all(
            select(_entries.c.entry)
            .where(
                _entries.c.project_key == project_key,
                _entries.c.session_id == session_id,
                _entries.c.subpath == subpath,
            )
            .order_by(_entries.c.id)
        )
        return [row["entry"] for row in rows]

    async def record_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        interface_type: str,
        conversation_id: str,
        subconversation_id: str | None,
        processing_profile_id: str,
        model: str,
        effort: str | None,
        system_prompt_sha256: str,
        taint_metadata: TaintMetadata | None,
    ) -> None:
        """Record that ``session_id`` ran ``turn_id`` and stored all of it.

        A turn re-run after an interruption replaces its earlier record.
        """
        row: dict[str, object] = {
            "session_id": session_id,
            "turn_id": turn_id,
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "subconversation_id": subconversation_id,
            "processing_profile_id": processing_profile_id,
            "model": model,
            "effort": effort,
            "system_prompt_sha256": system_prompt_sha256,
            "taint_metadata_json": taint_metadata,
            "created_at": datetime.now(UTC),
        }

        async def write(txn: DatabaseTransaction) -> None:
            await txn.execute(delete(_turns).where(_turns.c.turn_id == turn_id))
            await txn.execute(insert(_turns).values(**row))

        await self._db.atomic(write)

    async def latest_turn(
        self,
        *,
        interface_type: str,
        conversation_id: str,
        subconversation_id: str | None,
        processing_profile_id: str,
    ) -> ClaudeAgentSessionTurn | None:
        """The most recently recorded session turn in this conversation."""
        subconversation = (
            _turns.c.subconversation_id.is_(None)
            if subconversation_id is None
            else _turns.c.subconversation_id == subconversation_id
        )
        row = await self._db.fetch_one(
            select(_turns)
            .where(
                _turns.c.interface_type == interface_type,
                _turns.c.conversation_id == conversation_id,
                subconversation,
                _turns.c.processing_profile_id == processing_profile_id,
            )
            .order_by(_turns.c.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        return ClaudeAgentSessionTurn(
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            model=row["model"],
            effort=row["effort"],
            system_prompt_sha256=row["system_prompt_sha256"],
            taint_metadata_json=row["taint_metadata_json"],
        )

    async def delete_sessions_idle_since(self, cutoff: datetime) -> None:
        """Delete every session with no transcript entry newer than ``cutoff``."""
        idle = (
            select(_entries.c.session_id)
            .group_by(_entries.c.session_id)
            .having(func.max(_entries.c.created_at) < cutoff)
        )

        async def prune(txn: DatabaseTransaction) -> None:
            session_ids = [row["session_id"] for row in await txn.fetch_all(idle)]
            if not session_ids:
                return
            await txn.execute(
                delete(_turns).where(_turns.c.session_id.in_(session_ids))
            )
            await txn.execute(
                delete(_entries).where(_entries.c.session_id.in_(session_ids))
            )

        await self._db.atomic(prune)
