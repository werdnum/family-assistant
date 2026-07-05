"""Repository for notes storage operations."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql import functions as func

from family_assistant.skills.frontmatter import parse_frontmatter
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.base import BaseRepository


class NoteModel(BaseModel):
    """Note data returned by repository methods."""

    title: str
    content: str
    include_in_prompt: bool = True
    attachment_ids: list[str] = Field(default_factory=list)
    visibility_labels: list[str] = Field(default_factory=list)
    is_skill: bool = False
    skill_name: str | None = None
    skill_description: str | None = None


class NoteRow(NoteModel):
    """Full note row including database metadata, returned by get_by_id."""

    id: int
    created_at: datetime
    updated_at: datetime


def _parse_json_list(value: str | list[str] | None) -> list[str]:
    """Parse a JSON string to list of strings."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return []


# ast-grep-ignore: no-dict-any - dict[str, Any] from DatabaseContext.fetch_all/fetch_one
def _row_to_note_model(row: dict[str, Any]) -> NoteModel:
    """Convert a database row dict to a NoteModel."""
    return NoteModel(
        title=row["title"],
        content=row["content"],
        include_in_prompt=row["include_in_prompt"],
        attachment_ids=_parse_json_list(row["attachment_ids"]),
        visibility_labels=_parse_json_list(row["visibility_labels"]),
        is_skill=row.get("is_skill", False),
        skill_name=row.get("skill_name"),
        skill_description=row.get("skill_description"),
    )


class NoteNotFoundError(Exception):
    """Raised when a note cannot be found."""

    pass


class DuplicateNoteError(Exception):
    """Raised when attempting to create a note with a title that already exists."""

    pass


class NoteWritePolicyError(Exception):
    """Raised when a note write violates the active profile's write policy.

    Covers both the see-before-overwrite check (a restricted profile may not
    overwrite a note it cannot see) and the allowed-label ceiling.
    """

    pass


@dataclass(frozen=True)
class NoteWritePolicy:
    """Write-side visibility confinement for a note write.

    Enforced in the repository so every write path is covered, not just the
    ``add_or_update_note`` tool. Constructed once from the active profile (see
    ``ToolExecutionContext.note_write_policy``) and passed to every repository
    write.

    Attributes:
        visibility_grants: Grants used for the see-before-overwrite check. When
            None, the check is skipped (the caller may overwrite any note).
        default_labels: Applied when a *new* note omits ``visibility_labels``.
        required_labels: Write floor — always unioned into the final labels.
        allowed_labels: Write ceiling — when set, every final label must be a
            member, or the write is rejected.

    ``UNCONSTRAINED`` (all fields None) reproduces the pre-confinement behavior
    and is the explicit opt-out for trusted admin surfaces (e.g. the web notes
    API). Its use is restricted by an ast-grep conformance rule.
    """

    visibility_grants: set[str] | None
    default_labels: list[str] | None
    required_labels: list[str] | None
    allowed_labels: list[str] | None

    UNCONSTRAINED: ClassVar["NoteWritePolicy"]

    def resolve_labels(
        self,
        *,
        is_new_note: bool,
        requested_labels: list[str] | None,
        existing_labels: list[str],
    ) -> list[str]:
        """Compute the final visibility labels for a write, enforcing the ceiling.

        Raises:
            NoteWritePolicyError: if the resulting labels are not a subset of
                ``allowed_labels`` (when that ceiling is set), or if a confined
                policy (one with a floor or ceiling) targets an existing note
                whose current labels already fall outside that confinement.
        """
        # A confined writer may only update notes that are already inside its
        # confinement. Without this, updating a note that is currently
        # unrestricted (or otherwise visible) would append the required labels
        # and silently pull an unrelated user note into the quarantine space on
        # a title collision.
        if not is_new_note and (
            self.required_labels or self.allowed_labels is not None
        ):
            missing_floor = [
                label
                for label in (self.required_labels or [])
                if label not in existing_labels
            ]
            over_ceiling = (
                [
                    label
                    for label in existing_labels
                    if label not in set(self.allowed_labels)
                ]
                if self.allowed_labels is not None
                else []
            )
            if missing_floor or over_ceiling:
                raise NoteWritePolicyError(
                    "Cannot modify this note: its current visibility labels "
                    f"{sorted(existing_labels)} are outside the active profile's "
                    "write confinement "
                    f"(required: {sorted(self.required_labels or [])}, "
                    f"allowed: {sorted(self.allowed_labels) if self.allowed_labels is not None else 'any'}). "
                    "Choose a different title instead of relabeling an existing note."
                )

        if requested_labels is not None:
            base = list(requested_labels)
        elif is_new_note:
            base = list(self.default_labels) if self.default_labels else []
        else:
            base = list(existing_labels)

        final = list(base)
        if self.required_labels:
            for label in self.required_labels:
                if label not in final:
                    final.append(label)

        if self.allowed_labels is not None:
            allowed = set(self.allowed_labels)
            violations = sorted({label for label in final if label not in allowed})
            if violations:
                raise NoteWritePolicyError(
                    f"Visibility labels {violations} are not permitted by the active "
                    f"profile (allowed: {sorted(allowed)})."
                )

        return final


NoteWritePolicy.UNCONSTRAINED = NoteWritePolicy(
    visibility_grants=None,
    default_labels=None,
    required_labels=None,
    allowed_labels=None,
)


_NOTE_COLUMNS = [
    notes_table.c.title,
    notes_table.c.content,
    notes_table.c.include_in_prompt,
    notes_table.c.attachment_ids,
    notes_table.c.visibility_labels,
    notes_table.c.is_skill,
    notes_table.c.skill_name,
    notes_table.c.skill_description,
]


def _detect_skill_metadata(content: str) -> tuple[bool, str | None, str | None]:
    """Parse frontmatter to detect if content represents a skill.

    Returns (is_skill, skill_name, skill_description).
    """
    fm, _ = parse_frontmatter(content)
    if fm and "name" in fm and "description" in fm:
        return True, str(fm["name"]), str(fm["description"])
    return False, None, None


class NotesRepository(BaseRepository):
    """Repository for managing notes in the database."""

    def _apply_visibility_filter(
        self,
        stmt: sa.Select,  # type: ignore[type-arg]  # Generic Select type params are complex with dialect-specific expressions
        visibility_grants: set[str] | None,
    ) -> sa.Select:  # type: ignore[type-arg]  # Generic Select type params are complex with dialect-specific expressions
        """Apply visibility label filtering to a SELECT statement.

        When visibility_grants is None, no filtering is applied (backward compat).
        When set, only notes whose labels are a subset of the grants are returned.
        Notes with empty labels ([]) are always visible.
        """
        if visibility_grants is None:
            return stmt

        grants_list = sorted(visibility_grants)

        if self._db.engine.dialect.name == "postgresql":
            grants_json = json.dumps(grants_list)
            stmt = stmt.where(
                sa.cast(notes_table.c.visibility_labels, JSONB).contained_by(
                    sa.cast(sa.literal(grants_json), JSONB)
                )
            )
        else:
            # SQLite: empty labels always pass, non-empty checked with json_each
            stmt = stmt.where(
                sa.or_(
                    notes_table.c.visibility_labels == "[]",
                    ~sa.exists(
                        sa
                        .select(sa.literal(1))
                        .select_from(sa.func.json_each(notes_table.c.visibility_labels))
                        .where(sa.column("value").notin_(grants_list))
                    ),
                )
            )

        return stmt

    async def get_all(
        self,
        visibility_grants: set[str] | None,
    ) -> list[NoteModel]:
        """Retrieves all notes, optionally filtered by visibility grants."""
        try:
            stmt = select(*_NOTE_COLUMNS).order_by(notes_table.c.title)
            stmt = self._apply_visibility_filter(stmt, visibility_grants)
            rows = await self._db.fetch_all(stmt)
            return [_row_to_note_model(row) for row in rows]
        except SQLAlchemyError as e:
            self._logger.error(f"Database error in get_all: {e}", exc_info=True)
            raise

    async def get_prompt_notes(
        self,
        visibility_grants: set[str] | None,
    ) -> list[NoteModel]:
        """Retrieves only regular notes that should be included in prompts (excludes skills)."""
        try:
            stmt = (
                select(*_NOTE_COLUMNS)
                .where(notes_table.c.include_in_prompt.is_(True))
                .where(notes_table.c.is_skill.is_(False))
                .order_by(notes_table.c.title)
            )
            stmt = self._apply_visibility_filter(stmt, visibility_grants)
            rows = await self._db.fetch_all(stmt)
            return [_row_to_note_model(row) for row in rows]
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in get_prompt_notes: {e}", exc_info=True
            )
            raise

    async def get_excluded_notes_titles(
        self,
        visibility_grants: set[str] | None,
    ) -> list[str]:
        """Retrieves titles of regular notes that are excluded from prompts (excludes skills)."""
        try:
            stmt = (
                select(notes_table.c.title)
                .where(notes_table.c.include_in_prompt.is_(False))
                .where(notes_table.c.is_skill.is_(False))
                .order_by(notes_table.c.title)
            )
            stmt = self._apply_visibility_filter(stmt, visibility_grants)
            rows = await self._db.fetch_all(stmt)
            return [row["title"] for row in rows]
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in get_excluded_notes_titles: {e}", exc_info=True
            )
            raise

    async def get_skills(
        self,
        visibility_grants: set[str] | None,
    ) -> list[NoteModel]:
        """Retrieves notes that are skills, for building the skill catalog."""
        try:
            stmt = (
                select(*_NOTE_COLUMNS)
                .where(notes_table.c.is_skill.is_(True))
                .order_by(notes_table.c.skill_name)
            )
            stmt = self._apply_visibility_filter(stmt, visibility_grants)
            rows = await self._db.fetch_all(stmt)
            return [_row_to_note_model(row) for row in rows]
        except SQLAlchemyError as e:
            self._logger.error(f"Database error in get_skills: {e}", exc_info=True)
            raise

    async def get_by_id(
        self,
        note_id: int,
        visibility_grants: set[str] | None,
    ) -> NoteRow | None:
        """Retrieves a note by its ID.

        Args:
            note_id: The ID of the note to retrieve
            visibility_grants: If set, only return note if its labels are a subset

        Returns:
            NoteRow or None if not found/not accessible
        """
        query = select(notes_table).where(notes_table.c.id == note_id)
        query = self._apply_visibility_filter(query, visibility_grants)
        row = await self._db.fetch_one(query)
        if row:
            return NoteRow(
                id=row["id"],
                title=row["title"],
                content=row["content"],
                include_in_prompt=row["include_in_prompt"],
                attachment_ids=_parse_json_list(row["attachment_ids"]),
                visibility_labels=_parse_json_list(row["visibility_labels"]),
                is_skill=row.get("is_skill", False),
                skill_name=row.get("skill_name"),
                skill_description=row.get("skill_description"),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        return None

    async def get_by_title(
        self,
        title: str,
        visibility_grants: set[str] | None,
    ) -> NoteModel | None:
        """Retrieves a specific note by its title."""
        try:
            stmt = select(*_NOTE_COLUMNS).where(notes_table.c.title == title)
            stmt = self._apply_visibility_filter(stmt, visibility_grants)
            row = await self._db.fetch_one(stmt)
            if row:
                return _row_to_note_model(row)
            return None
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in get_by_title({title}): {e}", exc_info=True
            )
            raise

    async def add_or_update(
        self,
        title: str,
        content: str,
        include_in_prompt: bool = True,
        append: bool = False,
        attachment_ids: list[str] | None = None,
        visibility_labels: list[str] | None = None,
        *,
        write_policy: NoteWritePolicy,
    ) -> str:
        """Adds a new note or updates an existing note with the given title (upsert).

        Args:
            visibility_labels: Labels for visibility control.
                None = preserve existing on update, use default for new notes.
                Empty list = explicitly unrestricted (visible to all profiles).
            write_policy: Required. The active profile's write confinement.
                See-before-overwrite, default/required/allowed labels are applied
                here so every write path is covered. Pass
                ``NoteWritePolicy.UNCONSTRAINED`` from trusted admin surfaces.

        Raises:
            NoteWritePolicyError: if the caller cannot see an existing note with
                this title, or the resolved labels violate the allowed ceiling.
        """
        now = datetime.now(UTC)

        existing_note = await self.get_by_title(title, visibility_grants=None)

        # See-before-overwrite: a restricted profile may not overwrite a note it
        # cannot see. Skipped when the policy carries no grants (admin bypass).
        if existing_note is not None and write_policy.visibility_grants is not None:
            visible_existing = await self.get_by_title(
                title, visibility_grants=write_policy.visibility_grants
            )
            if visible_existing is None:
                raise NoteWritePolicyError(
                    f"Cannot modify note '{title}' - insufficient visibility permissions."
                )

        if append and existing_note:
            content = existing_note.content + "\n" + content

        # Determine attachment_ids to use
        if attachment_ids is None:
            attachment_ids_to_use = (
                existing_note.attachment_ids if existing_note else []
            )
        else:
            attachment_ids_to_use = attachment_ids

        visibility_labels_to_use = write_policy.resolve_labels(
            is_new_note=existing_note is None,
            requested_labels=visibility_labels,
            existing_labels=existing_note.visibility_labels if existing_note else [],
        )

        # Serialize to JSON strings
        attachment_ids_json = json.dumps(attachment_ids_to_use)
        visibility_labels_json = json.dumps(visibility_labels_to_use)

        # Detect skill metadata from frontmatter at write time
        is_skill, skill_name, skill_description = _detect_skill_metadata(content)

        if self._db.engine.dialect.name == "postgresql":
            # Use PostgreSQL's ON CONFLICT DO UPDATE for atomic upsert
            try:
                stmt = pg_insert(notes_table).values(
                    title=title,
                    content=content,
                    include_in_prompt=include_in_prompt,
                    attachment_ids=attachment_ids_json,
                    visibility_labels=visibility_labels_json,
                    is_skill=is_skill,
                    skill_name=skill_name,
                    skill_description=skill_description,
                    created_at=now,
                    updated_at=now,
                )
                # Define columns to update on conflict
                update_dict = {
                    "content": stmt.excluded.content,
                    "include_in_prompt": stmt.excluded.include_in_prompt,
                    "attachment_ids": stmt.excluded.attachment_ids,
                    "visibility_labels": stmt.excluded.visibility_labels,
                    "is_skill": stmt.excluded.is_skill,
                    "skill_name": stmt.excluded.skill_name,
                    "skill_description": stmt.excluded.skill_description,
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
                    attachment_ids=attachment_ids_json,
                    visibility_labels=visibility_labels_json,
                    is_skill=is_skill,
                    skill_name=skill_name,
                    skill_description=skill_description,
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
                            attachment_ids=attachment_ids_json,
                            visibility_labels=visibility_labels_json,
                            is_skill=is_skill,
                            skill_name=skill_name,
                            skill_description=skill_description,
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

    async def rename_and_update(
        self,
        original_title: str,
        new_title: str,
        content: str,
        include_in_prompt: bool,
        attachment_ids: list[str] | None = None,
        visibility_labels: list[str] | None = None,
        *,
        write_policy: NoteWritePolicy,
    ) -> str:
        """Renames a note and updates its content, preserving the primary key.

        Args:
            original_title: Current title of the note
            new_title: New title for the note
            content: Updated content
            include_in_prompt: Whether to include in prompt
            attachment_ids: Optional list of attachment IDs. If None, preserves existing.
            visibility_labels: Optional visibility labels. If None, preserves existing.
            write_policy: Required. The active profile's write confinement (see
                ``add_or_update``). Pass ``NoteWritePolicy.UNCONSTRAINED`` from
                trusted admin surfaces.

        Returns:
            Status message

        Raises:
            NoteNotFoundError: If original note not found
            DuplicateNoteError: If new title conflicts with existing note
            NoteWritePolicyError: If the caller cannot see the note being renamed
                or the resolved labels violate the allowed ceiling
            SQLAlchemyError: If database error occurs
        """
        try:
            # First verify the original note exists
            existing_note = await self.get_by_title(
                original_title, visibility_grants=None
            )
            if not existing_note:
                raise NoteNotFoundError(
                    f"Cannot rename because note '{original_title}' was not found"
                )

            # See-before-overwrite: a restricted profile may not rename a note it
            # cannot see. Skipped when the policy carries no grants (admin bypass).
            if write_policy.visibility_grants is not None:
                visible_existing = await self.get_by_title(
                    original_title, visibility_grants=write_policy.visibility_grants
                )
                if visible_existing is None:
                    raise NoteWritePolicyError(
                        f"Cannot modify note '{original_title}' - insufficient "
                        "visibility permissions."
                    )

            # Check if new title conflicts with existing note (unless it's the same note)
            if new_title != original_title:
                conflicting_note = await self.get_by_title(
                    new_title, visibility_grants=None
                )
                if conflicting_note:
                    raise DuplicateNoteError(
                        f"A note with title '{new_title}' already exists"
                    )

            # Determine attachment_ids to use
            if attachment_ids is None:
                attachment_ids_to_use = existing_note.attachment_ids
            else:
                attachment_ids_to_use = attachment_ids

            visibility_labels_to_use = write_policy.resolve_labels(
                is_new_note=False,
                requested_labels=visibility_labels,
                existing_labels=existing_note.visibility_labels,
            )

            # Serialize to JSON strings
            attachment_ids_json = json.dumps(attachment_ids_to_use)
            visibility_labels_json = json.dumps(visibility_labels_to_use)

            # Detect skill metadata from frontmatter at write time
            is_skill, skill_name, skill_description = _detect_skill_metadata(content)

            # Update the note in place, preserving the primary key
            stmt = (
                update(notes_table)
                .where(notes_table.c.title == original_title)
                .values(
                    title=new_title,
                    content=content,
                    include_in_prompt=include_in_prompt,
                    attachment_ids=attachment_ids_json,
                    visibility_labels=visibility_labels_json,
                    is_skill=is_skill,
                    skill_name=skill_name,
                    skill_description=skill_description,
                    updated_at=func.now(),
                )
            )

            result = await self._db.execute_with_retry(stmt)
            if result.rowcount > 0:  # type: ignore[attr-defined]
                self._logger.info(
                    f"Renamed note from '{original_title}' to '{new_title}'"
                )
                # Enqueue indexing task for the updated note
                await self._enqueue_indexing_task(new_title)
                return "Success"
            else:
                # This indicates the note was deleted between our check and update
                self._logger.warning(
                    f"No rows updated when renaming {original_title} - note may have been deleted"
                )
                raise NoteNotFoundError(
                    f"Note '{original_title}' not found (may have been deleted)"
                )

        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in rename_and_update({original_title} -> {new_title}): {e}",
                exc_info=True,
            )
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
                await self._db.tasks.enqueue(
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
