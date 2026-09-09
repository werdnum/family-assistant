"""Repository for stored scripts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from family_assistant.security.definition_records import (
    CreationDisposition,
    DefinitionArtifactKind,
    DefinitionGateOutcome,
    GateProvenance,
    definition_record_from_row,
    is_legacy_amnesty_record,
    legacy_amnesty_gate_outcome,
    legacy_authoring_taint_state,
    register_definition_write,
    script_definition_content,
    stamp_definition,
)
from family_assistant.storage.datetime_utils import normalize_datetime
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.scripts import scripts_table

if TYPE_CHECKING:
    from family_assistant.security.taint import TurnTaintState
    from family_assistant.storage.database import DatabaseTransaction


class ScriptModel(BaseModel):
    """Script data returned by repository methods."""

    name: str
    description: str
    script_code: str
    # ast-grep-ignore: no-dict-any - JSON Schema parameter is genuinely arbitrary, must accept any valid JSON schema
    parameters_schema: dict[str, Any] | None = None


class ScriptRow(ScriptModel):
    """Full script row including database metadata."""

    id: int
    created_at: datetime
    updated_at: datetime
    definition_record: str | None = None
    """The stored definition record, as written: JSON text, parsed at resolution."""


class ScriptNotFoundError(Exception):
    """Raised when a script cannot be found."""


class ScriptsRepository(BaseRepository):
    """Repository for managing stored scripts."""

    async def save(
        self,
        name: str,
        description: str,
        script_code: str,
        # ast-grep-ignore: no-dict-any - JSON Schema parameter is genuinely arbitrary
        parameters_schema: dict[str, Any] | None = None,
        *,
        definition_taint_state: TurnTaintState | None = None,
        definition_gate: DefinitionGateOutcome | None = None,
        definition_human_direct: bool = False,
    ) -> ScriptRow:
        """Save or update a script (upsert by name).

        Args:
            name: The script name (unique identifier)
            description: Description of the script
            script_code: The script code/content
            parameters_schema: Optional JSON Schema for expected parameters

        Returns:
            ScriptRow with the saved script data

        Raises:
            SQLAlchemyError: If database operation fails
        """
        now = datetime.now(UTC)
        schema_json = (
            json.dumps(parameters_schema) if parameters_schema is not None else None
        )
        # A save replaces the whole body, so there is nothing retained to merge:
        # the arguments are already the complete post-mutation definition.
        definition_record = json.dumps(
            stamp_definition(
                content=script_definition_content(
                    name=name,
                    description=description,
                    script_code=script_code,
                    parameters_schema=parameters_schema,
                ),
                taint_state=definition_taint_state,
                gate_outcome=definition_gate,
                human_direct=definition_human_direct,
            ).to_dict()
        )
        register_definition_write(
            definition_gate,
            definition_record,
            kind=DefinitionArtifactKind.SCRIPT,
            artifact_id=name,
        )

        if self._db.dialect_name == "postgresql":
            stmt = pg_insert(scripts_table).values(
                name=name,
                description=description,
                script_code=script_code,
                parameters_schema=schema_json,
                created_at=now,
                updated_at=now,
                definition_record=definition_record,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "description": stmt.excluded.description,
                    "script_code": stmt.excluded.script_code,
                    "parameters_schema": stmt.excluded.parameters_schema,
                    "updated_at": stmt.excluded.updated_at,
                    "definition_record": stmt.excluded.definition_record,
                },
            )
            await self._db.execute(stmt)
        else:
            # SQLite: try insert, then update on conflict
            try:
                stmt = insert(scripts_table).values(
                    name=name,
                    description=description,
                    script_code=script_code,
                    parameters_schema=schema_json,
                    created_at=now,
                    updated_at=now,
                    definition_record=definition_record,
                )
                await self._db.execute(stmt)
            except IntegrityError:
                stmt = (
                    update(scripts_table)
                    .where(scripts_table.c.name == name)
                    .values(
                        description=description,
                        script_code=script_code,
                        parameters_schema=schema_json,
                        updated_at=now,
                        definition_record=definition_record,
                    )
                )
                await self._db.execute(stmt)

        # Fetch and return the saved script
        return await self.get_by_name(name)  # type: ignore[return-value] # After save, script always exists

    async def attach_definition_verdict(
        self,
        name: str,
        *,
        write_id: str,
        disposition: CreationDisposition,
        gate: GateProvenance,
    ) -> bool:
        """Attach an asynchronously computed verdict to this definition's record.

        Under ``observe`` the reviewer runs off the critical path, so the write
        lands before its verdict exists and the verdict arrives here. The write
        id guards the update, read and write in one transaction: the row must
        still hold the exact write the verdict judged, so a mutation racing the
        review -- an identical rewrite from another turn included -- leaves the
        new content awaiting its own verdict rather than inheriting this one.

        Returns whether the verdict was attached.
        """

        async def body(txn: DatabaseTransaction) -> bool:
            # Locked, not merely re-read: on PostgreSQL a concurrent write
            # committing between the check and the update would otherwise be
            # overwritten by the record this read returned -- reverting an edit
            # while reporting the verdict attached. SQLite serializes writes on
            # the engine lock and ignores the clause.
            row = await txn.fetch_one(
                select(scripts_table.c.definition_record)
                .where(scripts_table.c.name == name)
                .with_for_update()
            )
            record = definition_record_from_row(
                row["definition_record"] if row is not None else None
            )
            if record is None or record.pending_write_id != write_id:
                return False
            await txn.execute(
                update(scripts_table)
                .where(scripts_table.c.name == name)
                .values(
                    # ast-grep-ignore: no-unstamped-executable-definition-write - verdict attach: with_verdict() derives from the stored record, leaving stamp and hash untouched
                    definition_record=json.dumps(
                        record.with_verdict(disposition, gate).to_dict()
                    )
                )
            )
            return True

        return await self._db.atomic(body)

    async def list_unstamped_definitions(
        self,
        *,
        created_before: datetime,
    ) -> list[ScriptRow]:
        """List stored scripts that hold no definition record and predate a cutoff.

        The candidate set for an operator's legacy amnesty (see
        ``docs/design/legacy-definition-amnesty.md``). Absence is read from the
        row rather than asked of SQL, because a JSON column stores a written
        ``None`` as JSON null rather than SQL NULL and an ``IS NULL`` predicate
        would then miss a record that was cleared. It stays absence, not
        unreadability: a script holding *any* record -- cured, uncured, or void through a
        hash mismatch -- is never a candidate.

        Creation, not last modification, is the test, as for the other two
        definition classes: every save stamps, so a script created before the
        cutoff and still holding no record has not been saved since. Reading
        ``updated_at`` instead would additionally make an amnesty unrepeatable
        after a revocation, since both writes touch it.
        """
        stmt = (
            select(scripts_table)
            .where(scripts_table.c.created_at < created_before)
            .order_by(scripts_table.c.name)
        )
        rows = await self._db.fetch_all(stmt)
        return [
            _row_to_script_row(dict(row))
            for row in rows
            if row["definition_record"] is None
        ]

    async def list_amnestied_definitions(self) -> list[ScriptRow]:
        """List stored scripts currently holding an operator's amnesty.

        Filtered in Python rather than in SQL: the record is a JSON document
        whose disposition each backend would have to be asked for differently,
        and a household's script estate is small enough that reading it is
        cheaper than maintaining two dialects of the same predicate.
        """
        stmt = (
            select(scripts_table)
            .where(scripts_table.c.definition_record.is_not(None))
            .order_by(scripts_table.c.name)
        )
        rows = await self._db.fetch_all(stmt)
        return [
            _row_to_script_row(dict(row))
            for row in rows
            if is_legacy_amnesty_record(row["definition_record"])
        ]

    async def amnesty_legacy_definition(
        self,
        name: str,
        *,
        created_before: datetime,
    ) -> bool:
        """Record an operator's amnesty for a script that predates stamping.

        Reads the body and writes the record in one transaction, so the hash
        covers exactly the code that was amnestied. Both eligibility conditions
        are re-checked under the lock rather than trusted from the listing: a
        record written since is never overwritten, and a script that is not
        pre-cutoff is never amnestied.

        Returns whether the amnesty was recorded.
        """

        async def body(txn: DatabaseTransaction) -> bool:
            row = await txn.fetch_one(
                select(scripts_table)
                .where(scripts_table.c.name == name)
                .with_for_update()
            )
            if row is None or row["definition_record"] is not None:
                return False
            created_at = normalize_datetime(row["created_at"])
            if created_at is None or created_at >= created_before:
                return False
            script = _row_to_script_row(dict(row))
            definition_record = json.dumps(
                stamp_definition(
                    content=script_definition_content(
                        name=script.name,
                        description=script.description,
                        script_code=script.script_code,
                        parameters_schema=script.parameters_schema,
                    ),
                    taint_state=legacy_authoring_taint_state(),
                    gate_outcome=legacy_amnesty_gate_outcome(),
                ).to_dict()
            )
            await txn.execute(
                update(scripts_table)
                .where(scripts_table.c.name == name)
                .values(definition_record=definition_record)
            )
            return True

        return await self._db.atomic(body)

    async def revoke_legacy_amnesty(self, name: str) -> bool:
        """Clear an operator's amnesty, restoring the fail-closed legacy state.

        Only an amnesty record is cleared: a judge verdict, a human
        confirmation, or a genuinely tainted stamp is left alone, so revocation
        can never be the way a real record is deleted.

        Returns whether an amnesty record was cleared.
        """

        async def body(txn: DatabaseTransaction) -> bool:
            row = await txn.fetch_one(
                select(scripts_table.c.definition_record)
                .where(scripts_table.c.name == name)
                .with_for_update()
            )
            if not is_legacy_amnesty_record(
                row["definition_record"] if row is not None else None
            ):
                return False
            await txn.execute(
                update(scripts_table)
                .where(scripts_table.c.name == name)
                .values(definition_record=None)
            )
            return True

        return await self._db.atomic(body)

    async def get_by_name(self, name: str) -> ScriptRow | None:
        """Get a script by name.

        Args:
            name: The script name to retrieve

        Returns:
            ScriptRow if found, None otherwise

        Raises:
            SQLAlchemyError: If database operation fails
        """
        try:
            stmt = select(scripts_table).where(scripts_table.c.name == name)
            row = await self._db.fetch_one(stmt)
            if row is None:
                return None
            return _row_to_script_row(row)
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_by_name({name}): {e}")
            raise

    async def get_by_id(self, script_id: int) -> ScriptRow | None:
        """Get a script by ID.

        Args:
            script_id: The script ID to retrieve

        Returns:
            ScriptRow if found, None otherwise

        Raises:
            SQLAlchemyError: If database operation fails
        """
        try:
            stmt = select(scripts_table).where(scripts_table.c.id == script_id)
            row = await self._db.fetch_one(stmt)
            if row is None:
                return None
            return _row_to_script_row(row)
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_by_id({script_id}): {e}")
            raise

    async def list_all(self) -> list[ScriptRow]:
        """List all scripts ordered by name.

        Returns:
            List of ScriptRow objects

        Raises:
            SQLAlchemyError: If database operation fails
        """
        try:
            stmt = select(scripts_table).order_by(scripts_table.c.name)
            rows = await self._db.fetch_all(stmt)
            return [_row_to_script_row(row) for row in rows]
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in list_all: {e}")
            raise

    async def delete(self, name: str) -> bool:
        """Delete a script by name.

        Args:
            name: The script name to delete

        Returns:
            True if deleted, False if not found

        Raises:
            SQLAlchemyError: If database operation fails
        """
        stmt = delete(scripts_table).where(scripts_table.c.name == name)
        try:
            result = await self._db.execute(stmt)
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in delete({name}): {e}")
            raise

        deleted = result.rowcount > 0  # type: ignore[union-attr] # rowcount is available on CursorResult
        if deleted:
            self._logger.info(f"Deleted script: {name}")
        else:
            self._logger.warning(f"Script not found for deletion: {name}")
        return deleted


# ast-grep-ignore: no-dict-any - JSON Schema can have any valid structure
def _parse_parameters_schema(
    # ast-grep-ignore: no-dict-any - JSON Schema is genuinely arbitrary
    value: str | dict[str, Any] | None,
    # ast-grep-ignore: no-dict-any - Function returns parsed JSON Schema which is genuinely arbitrary
) -> dict[str, Any] | None:
    """Parse parameters schema from JSON string or dict.

    Args:
        value: JSON string, dict, or None

    Returns:
        Parsed dict or None if value is None or invalid
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


# ast-grep-ignore: no-dict-any - dict[str, Any] from Database.fetch_one
def _row_to_script_row(row: dict[str, Any]) -> ScriptRow:
    """Convert a database row dict to a ScriptRow.

    Args:
        row: Raw database row dictionary

    Returns:
        ScriptRow with parsed data
    """
    return ScriptRow(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        script_code=row["script_code"],
        parameters_schema=_parse_parameters_schema(row.get("parameters_schema")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        definition_record=row.get("definition_record"),
    )
