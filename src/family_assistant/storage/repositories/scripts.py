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
    register_definition_write,
    script_definition_content,
    stamp_definition,
)
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
