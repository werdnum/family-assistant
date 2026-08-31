"""Repository for stored scripts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.scripts import scripts_table


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

        if self._db.dialect_name == "postgresql":
            stmt = pg_insert(scripts_table).values(
                name=name,
                description=description,
                script_code=script_code,
                parameters_schema=schema_json,
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "description": stmt.excluded.description,
                    "script_code": stmt.excluded.script_code,
                    "parameters_schema": stmt.excluded.parameters_schema,
                    "updated_at": stmt.excluded.updated_at,
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
                    )
                )
                await self._db.execute(stmt)

        # Fetch and return the saved script
        return await self.get_by_name(name)  # type: ignore[return-value] # After save, script always exists

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
    )
