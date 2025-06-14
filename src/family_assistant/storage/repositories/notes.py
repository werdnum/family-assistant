"""Repository for notes storage operations."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from family_assistant import storage as storage_module
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.base import BaseRepository


class NotesRepository(BaseRepository):
    """Repository for managing notes in the database."""

    async def get_all(self) -> list[dict[str, Any]]:
        """Retrieves all notes."""
        try:
            stmt = select(
                notes_table.c.title,
                notes_table.c.content,
                notes_table.c.include_in_prompt,
            ).order_by(notes_table.c.title)
            rows = await self._db.fetch_all(stmt)
            return [
                {
                    "title": row["title"],
                    "content": row["content"],
                    "include_in_prompt": row["include_in_prompt"],
                }
                for row in rows
            ]
        except SQLAlchemyError as e:
            self._logger.error(f"Database error in get_all: {e}", exc_info=True)
            raise

    async def get_prompt_notes(self) -> list[dict[str, str]]:
        """Retrieves only notes that should be included in prompts."""
        try:
            stmt = (
                select(notes_table.c.title, notes_table.c.content)
                .where(notes_table.c.include_in_prompt.is_(True))
                .order_by(notes_table.c.title)
            )
            rows = await self._db.fetch_all(stmt)
            return [{"title": row["title"], "content": row["content"]} for row in rows]
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in get_prompt_notes: {e}", exc_info=True
            )
            raise

    async def get_by_title(self, title: str) -> dict[str, Any] | None:
        """Retrieves a specific note by its title."""
        try:
            stmt = select(
                notes_table.c.title,
                notes_table.c.content,
                notes_table.c.include_in_prompt,
            ).where(notes_table.c.title == title)
            row = await self._db.fetch_one(stmt)
            return row if row else None
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in get_by_title({title}): {e}", exc_info=True
            )
            raise

    async def get_by_id(self, note_id: int) -> dict[str, Any] | None:
        """Retrieves a specific note by its ID."""
        try:
            stmt = select(
                notes_table.c.id,
                notes_table.c.title,
                notes_table.c.content,
                notes_table.c.include_in_prompt,
                notes_table.c.created_at,
                notes_table.c.updated_at,
            ).where(notes_table.c.id == note_id)
            row = await self._db.fetch_one(stmt)
            return dict(row) if row else None
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in get_by_id({note_id}): {e}", exc_info=True
            )
            raise

    async def add_or_update(
        self,
        title: str,
        content: str,
        include_in_prompt: bool = True,
    ) -> str:
        """Adds a new note or updates an existing note with the given title (upsert)."""
        now = datetime.now(timezone.utc)

        if self._db.engine.dialect.name == "postgresql":
            # Use PostgreSQL's ON CONFLICT DO UPDATE for atomic upsert
            try:
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                stmt = pg_insert(notes_table).values(
                    title=title,
                    content=content,
                    include_in_prompt=include_in_prompt,
                    created_at=now,
                    updated_at=now,
                )
                # Define columns to update on conflict
                update_dict = {
                    "content": stmt.excluded.content,
                    "include_in_prompt": stmt.excluded.include_in_prompt,
                    "updated_at": stmt.excluded.updated_at,
                }
                stmt = stmt.on_conflict_do_update(
                    index_elements=["title"],  # The unique constraint column
                    set_=update_dict,
                )
                # Use execute_with_retry as commit is handled by context manager
                await self._db.execute_with_retry(stmt)
                self._logger.info(
                    f"Successfully added/updated note: {title} (using ON CONFLICT)"
                )

                # Enqueue indexing task
                await self._enqueue_indexing_task(title)
                return "Success"
            except SQLAlchemyError as e:
                self._logger.error(
                    f"PostgreSQL error in add_or_update({title}): {e}", exc_info=True
                )
                raise

        else:
            # Fallback for SQLite and other dialects: Try INSERT, then UPDATE on IntegrityError.
            try:
                # Attempt INSERT first
                insert_stmt = insert(notes_table).values(
                    title=title,
                    content=content,
                    include_in_prompt=include_in_prompt,
                    created_at=now,
                    updated_at=now,
                )
                await self._db.execute_with_retry(insert_stmt)
                self._logger.info(f"Inserted new note: {title} (SQLite fallback)")

                # Enqueue indexing task
                await self._enqueue_indexing_task(title)
                return "Success"
            except SQLAlchemyError as e:
                # Check specifically for unique constraint violation
                if isinstance(e, IntegrityError):
                    self._logger.info(
                        f"Note '{title}' already exists (SQLite fallback), attempting update."
                    )
                    # Perform UPDATE if INSERT failed due to unique constraint
                    update_stmt = (
                        update(notes_table)
                        .where(notes_table.c.title == title)
                        .values(
                            content=content,
                            include_in_prompt=include_in_prompt,
                            updated_at=now,
                        )
                    )
                    # Execute update within the same transaction context
                    result = await self._db.execute_with_retry(update_stmt)
                    if result.rowcount == 0:  # type: ignore[attr-defined]
                        # This could happen if the note was deleted between the failed INSERT and this UPDATE
                        self._logger.error(
                            f"Update failed for note '{title}' after insert conflict (SQLite fallback). Note might have been deleted concurrently."
                        )
                        # Re-raise the original error or a custom one
                        raise RuntimeError(
                            f"Failed to update note '{title}' after insert conflict."
                        ) from e
                    self._logger.info(f"Updated note: {title} (SQLite fallback)")

                    # Enqueue indexing task
                    await self._enqueue_indexing_task(title)
                    return "Success"
                else:
                    # Re-raise other SQLAlchemy errors
                    self._logger.error(
                        f"Database error during INSERT in add_or_update({title}) (SQLite fallback): {e}",
                        exc_info=True,
                    )
                    raise e

    async def delete(self, title: str) -> bool:
        """Deletes a note by title."""
        try:
            stmt = delete(notes_table).where(notes_table.c.title == title)
            # Use execute_with_retry as commit is handled by context manager
            result = await self._db.execute_with_retry(stmt)
            deleted_count = result.rowcount  # type: ignore[attr-defined]
            if deleted_count > 0:
                self._logger.info(f"Deleted note: {title}")
                return True
            else:
                self._logger.warning(f"Note not found for deletion: {title}")
                return False
        except SQLAlchemyError as e:
            self._logger.error(f"Database error in delete({title}): {e}", exc_info=True)
            raise

    async def _enqueue_indexing_task(self, title: str) -> None:
        """
        Helper function to enqueue an indexing task for a note.

        Args:
            title: Title of the note to index
        """
        try:
            # Fetch the note to get its ID
            note_stmt = select(notes_table.c.id).where(notes_table.c.title == title)
            note_row = await self._db.fetch_one(note_stmt)
            if note_row:
                # Use UUID to ensure unique task IDs for re-indexing
                await storage_module.enqueue_task(
                    db_context=self._db,
                    task_id=f"index_note_{note_row['id']}_{uuid.uuid4()}",
                    task_type="index_note",
                    payload={"note_id": note_row["id"]},
                )
                self._logger.info(
                    f"Enqueued indexing task for note ID {note_row['id']} (title: {title})"
                )
        except Exception as e:
            self._logger.error(
                f"Failed to enqueue indexing task for note '{title}': {e}"
            )
            # Don't fail the note operation if indexing task enqueueing fails
