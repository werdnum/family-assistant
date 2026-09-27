"""Read and write stamps for assistant-authored calendar events."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from family_assistant.security.taint import TaintMetadata
from family_assistant.storage.calendar_provenance import (
    CalendarProvenanceRecord,
    calendar_event_provenance_table,
)
from family_assistant.storage.repositories.base import BaseRepository


class CalendarProvenanceRepository(BaseRepository):
    """Provenance lookup for event markers carried by remote calendar data."""

    async def record(
        self,
        *,
        marker: str,
        owner_user_id: str | None,
        source_key: str,
        event_uid: str,
        event_version: str,
        taint_metadata: TaintMetadata,
    ) -> None:
        """Store a successful remote write's version and inherited taint."""
        values = {
            "marker": marker,
            "owner_user_id": owner_user_id,
            "source_key": source_key,
            "event_uid": event_uid,
            "event_version": event_version,
            "taint_metadata": dict(taint_metadata),
            "updated_at": datetime.now(UTC),
        }
        if self._db.dialect_name == "postgresql":
            stmt = pg_insert(calendar_event_provenance_table).values(**values)
        else:
            stmt = sqlite_insert(calendar_event_provenance_table).values(**values)
        await self._db.execute(
            stmt.on_conflict_do_update(
                index_elements=["marker"],
                set_={key: value for key, value in values.items() if key != "marker"},
            )
        )

    async def get_many(
        self, markers: Sequence[str]
    ) -> dict[str, CalendarProvenanceRecord]:
        """Find stamps in one query for a mixed-source calendar search."""
        if not markers:
            return {}
        rows = await self._db.fetch_all(
            sa.select(calendar_event_provenance_table).where(
                calendar_event_provenance_table.c.marker.in_(markers)
            )
        )
        return {
            str(row["marker"]): cast("CalendarProvenanceRecord", dict(row))
            for row in rows
        }

    async def known_event_uids(
        self, event_uids: Sequence[str]
    ) -> set[tuple[str, str, str | None]]:
        """Detect a removed marker on an event previously written by the assistant."""
        if not event_uids:
            return set()
        rows = await self._db.fetch_all(
            sa.select(
                calendar_event_provenance_table.c.source_key,
                calendar_event_provenance_table.c.event_uid,
                calendar_event_provenance_table.c.owner_user_id,
            ).where(
                calendar_event_provenance_table.c.event_uid.in_(event_uids),
            )
        )
        return {
            (str(row["source_key"]), str(row["event_uid"]), row["owner_user_id"])
            for row in rows
        }
