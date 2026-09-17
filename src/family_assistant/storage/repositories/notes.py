"""Repository for notes storage operations."""

import json
import uuid
from collections.abc import Iterable, Mapping
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

from family_assistant.memory.index import TopicIndexEntry, regenerate_topic_index
from family_assistant.memory.invariants import (
    MEMORY_LABEL,
    MemoryWriteError,
    enforce_memory_invariants,
    is_memory_write,
)
from family_assistant.skills.frontmatter import parse_frontmatter
from family_assistant.storage.database import DatabaseExecutor, DatabaseTransaction
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.tasks import TaskPriority


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
    # ast-grep-ignore: no-dict-any - provenance metadata stores compact runtime taint JSON
    provenance_metadata: dict[str, Any] | None = None


class NoteRow(NoteModel):
    """Full note row including database metadata, returned by get_by_id."""

    id: int
    created_at: datetime
    updated_at: datetime


class MemoryTopicNote(BaseModel):
    """One memory topic note as a review reads it: its text and its recency.

    Narrower than :class:`NoteModel` because a review needs nothing else, and
    ``updated_at`` because when a topic last changed is how the request orders
    topics when they do not all fit.
    """

    title: str
    content: str
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


def _merge_unique_labels(
    base_labels: list[str],
    additional_labels: list[str] | None,
) -> list[str]:
    if not additional_labels:
        return base_labels
    merged = list(base_labels)
    for label in additional_labels:
        if label not in merged:
            merged.append(label)
    return merged


# ast-grep-ignore: no-dict-any - dict[str, Any] from Database.fetch_all/fetch_one
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
        provenance_metadata=row.get("provenance_metadata_json"),
    )


class NoteNotFoundError(Exception):
    """Raised when a note cannot be found."""


class DuplicateNoteError(Exception):
    """Raised when attempting to create a note with a title that already exists."""


class NoteWritePolicyError(Exception):
    """Raised when a note write violates the active profile's write policy.

    Covers both the see-before-overwrite check (a restricted profile may not
    overwrite a note it cannot see) and the allowed-label ceiling.
    """


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
        denied_labels: Named spaces this writer may not touch, whatever the
            ceiling says. The mirror of ``NoteReadPolicy.denied_labels``, and
            the reason it is not just an ``allowed_labels`` entry: a writer
            with no ceiling has no list to leave a label out of.

    ``UNCONSTRAINED`` (all fields None or empty) reproduces the pre-confinement
    behavior and is the explicit opt-out for trusted admin surfaces (e.g. the
    web notes API). Its use is restricted by an ast-grep conformance rule.
    """

    visibility_grants: set[str] | None
    default_labels: list[str] | None
    required_labels: list[str] | None
    allowed_labels: list[str] | None
    denied_labels: frozenset[str] = frozenset()

    UNCONSTRAINED: ClassVar["NoteWritePolicy"]

    def see_before_overwrite_read_policy(self) -> "NoteReadPolicy":
        """The read this policy's see-before-overwrite check performs.

        Grants only, no floor: the question is whether the writer can *see* the
        note it is about to overwrite, which its grants answer. The write floor
        is applied separately, by ``resolve_labels`` and the conflict-update
        predicate. Callers that mirror the check ahead of the write (the
        confirmation preview) take it from here rather than rebuilding it, so
        the preview cannot drift from what the repository will do.
        """
        return NoteReadPolicy(
            grants=(
                None
                if self.visibility_grants is None
                else frozenset(self.visibility_grants)
            ),
            denied_labels=self.denied_labels,
        )

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
                ``allowed_labels`` (when that ceiling is set), if the write
                would create or touch a note in a denied space, or if a
                confined policy (one with a floor or ceiling) targets an
                existing note whose current labels already fall outside that
                confinement.
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

        # Checked against the note's current labels as well as the write's own:
        # a writer that cannot see a denied note must not be able to reach it by
        # submitting a label set that omits the label it already carries.
        blocked = sorted(self.denied_labels & (set(final) | set(existing_labels)))
        if blocked:
            raise NoteWritePolicyError(
                f"Visibility labels {blocked} are withheld from the active "
                "profile, which cannot read the notes that carry them. Writing "
                "one blind is refused."
            )

        return final


NoteWritePolicy.UNCONSTRAINED = NoteWritePolicy(
    visibility_grants=None,
    default_labels=None,
    required_labels=None,
    allowed_labels=None,
)


@dataclass(frozen=True)
class NoteReadPolicy:
    """Read-side visibility confinement for a profile's note and skill reads.

    The read-side mirror of :class:`NoteWritePolicy`'s required labels, and the
    one object both note-resolution boundaries consult: the notes repository
    for stored notes and :class:`~family_assistant.skills.registry.NoteRegistry`
    for file-based skills. Grants alone cannot confine a reader, because a note
    is visible when its labels are a *subset* of the grants -- so an unlabelled
    note, and every label-less file skill, is visible to every reader including
    one granted a single label. ``required_labels`` is what closes that: a row
    is admitted only when it also carries every required label.

    Attributes:
        grants: The reader's visibility grants. When None the subset check is
            skipped (the reader sees every label set).
        required_labels: Read floor -- a note or skill must carry all of these
            to be admitted. Empty means no floor, the ordinary reader.
        denied_labels: Read ceiling -- a note or skill carrying any of these is
            refused whatever the grants say. Grants cannot express this on
            their own: a reader with no grants configured sees every label set,
            so there is no list to leave a label out of.

    ``UNRESTRICTED`` (no grants, no floor, no ceiling) is the explicit opt-out
    for admin surfaces that manage notes rather than read them as a profile.
    Its use is restricted by an ast-grep conformance rule.
    """

    grants: frozenset[str] | None
    required_labels: frozenset[str] = frozenset()
    denied_labels: frozenset[str] = frozenset()

    UNRESTRICTED: ClassVar["NoteReadPolicy"]

    @classmethod
    def for_profile(
        cls,
        *,
        visibility_grants: Iterable[str] | None,
        required_labels: Iterable[str] | None,
        memory_read: bool,
    ) -> "NoteReadPolicy":
        """Build the policy a profile's reads run under, from its config.

        This is the one place ``memory_read`` becomes visibility, so the
        setting and the grant can never disagree. A profile that reads memory
        is granted the ``memory`` label whether or not an operator listed it;
        a profile that does not is denied it whether or not an operator did.
        "A deployment can turn reading off" is then one switch rather than a
        switch and a list that have to be kept in step.
        """
        grants = None if visibility_grants is None else frozenset(visibility_grants)
        if memory_read and grants is not None:
            grants |= {MEMORY_LABEL}
        return cls(
            grants=grants,
            required_labels=frozenset(required_labels or ()),
            denied_labels=frozenset() if memory_read else frozenset({MEMORY_LABEL}),
        )

    def admits_labels(self, labels: Iterable[str]) -> bool:
        """Whether an object carrying ``labels`` is readable under this policy.

        Used by the boundaries that hold their objects in memory rather than in
        the notes table -- file-based skills -- so they apply the same rule the
        SQL conditions apply to rows.
        """
        label_set = frozenset(labels)
        if self.grants is not None and not label_set <= self.grants:
            return False
        if self.denied_labels & label_set:
            return False
        return self.required_labels <= label_set


NoteReadPolicy.UNRESTRICTED = NoteReadPolicy(grants=None, required_labels=frozenset())


_NOTE_COLUMNS = [
    notes_table.c.title,
    notes_table.c.content,
    notes_table.c.include_in_prompt,
    notes_table.c.attachment_ids,
    notes_table.c.visibility_labels,
    notes_table.c.is_skill,
    notes_table.c.skill_name,
    notes_table.c.skill_description,
    notes_table.c.provenance_metadata_json,
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

    def _apply_read_policy(
        self,
        stmt: sa.Select,  # type: ignore[type-arg]  # Generic Select type params are complex with dialect-specific expressions
        read_policy: NoteReadPolicy,
    ) -> sa.Select:  # type: ignore[type-arg]  # Generic Select type params are complex with dialect-specific expressions
        """Constrain a SELECT to the rows ``read_policy`` admits.

        Three clauses, and each does something the others cannot. The grants
        clause keeps a reader out of notes labelled beyond what it was granted;
        the required-labels clause keeps it out of everything that is *not*
        labelled for it, which the grants clause cannot do because an
        unlabelled note is a subset of every grant set; the denied-labels
        clause keeps a reader with no grants at all out of a named space, which
        the grants clause cannot do because there is no list to omit from.
        """
        if read_policy.grants is not None:
            stmt = stmt.where(self._labels_subset_condition(sorted(read_policy.grants)))
        if read_policy.required_labels:
            stmt = stmt.where(
                self._labels_superset_condition(sorted(read_policy.required_labels))
            )
        for label in sorted(read_policy.denied_labels):
            stmt = stmt.where(sa.not_(self._labels_superset_condition([label])))
        return stmt

    def _labels_subset_condition(
        self, target_labels: list[str]
    ) -> sa.ColumnElement[bool]:
        """SQL condition: the row's visibility labels are a subset of ``target_labels``.

        Empty labels ([]) always pass (the empty set is a subset of anything).
        """
        if self._db.dialect_name == "postgresql":
            return sa.cast(notes_table.c.visibility_labels, JSONB).contained_by(
                sa.cast(sa.literal(json.dumps(target_labels)), JSONB)
            )
        if not target_labels:
            return notes_table.c.visibility_labels == "[]"
        # SQLite: empty labels always pass, non-empty checked with json_each
        return sa.or_(
            notes_table.c.visibility_labels == "[]",
            ~sa.exists(
                sa
                .select(sa.literal(1))
                .select_from(sa.func.json_each(notes_table.c.visibility_labels))
                .where(sa.column("value").notin_(target_labels))
            ),
        )

    def _labels_superset_condition(
        self, required_labels: list[str]
    ) -> sa.ColumnElement[bool]:
        """SQL condition: the row's visibility labels contain every required label."""
        if self._db.dialect_name == "postgresql":
            return sa.cast(notes_table.c.visibility_labels, JSONB).contains(
                sa.cast(sa.literal(json.dumps(required_labels)), JSONB)
            )
        return sa.and_(*[
            sa.exists(
                sa
                .select(sa.literal(1))
                .select_from(sa.func.json_each(notes_table.c.visibility_labels))
                .where(sa.column("value") == label)
            )
            for label in required_labels
        ])

    def _writable_under_policy_condition(
        self, write_policy: NoteWritePolicy
    ) -> sa.ColumnElement[bool] | None:
        """SQL predicate: an existing row may be overwritten under ``write_policy``.

        Mirrors the preflight checks (see-before-overwrite plus the
        current-label confinement of ``resolve_labels``) so they hold
        *atomically* with the write: applied as the conflict-update WHERE, a
        same-title row inserted by another transaction after the preflight
        cannot be overwritten if the preflight would have rejected it. None
        means the policy places no constraint on overwrites (unconditional
        update, the pre-confinement behavior).
        """
        conditions: list[sa.ColumnElement[bool]] = []
        if write_policy.visibility_grants is not None:
            conditions.append(
                self._labels_subset_condition(sorted(write_policy.visibility_grants))
            )
        if write_policy.required_labels:
            conditions.append(
                self._labels_superset_condition(list(write_policy.required_labels))
            )
        if write_policy.allowed_labels is not None:
            conditions.append(
                self._labels_subset_condition(sorted(write_policy.allowed_labels))
            )
        if not conditions:
            return None
        return sa.and_(*conditions)

    async def get_all(
        self,
        *,
        read_policy: NoteReadPolicy,
    ) -> list[NoteModel]:
        """Retrieves every note the read policy admits."""
        try:
            stmt = select(*_NOTE_COLUMNS).order_by(notes_table.c.title)
            stmt = self._apply_read_policy(stmt, read_policy)
            rows = await self._db.fetch_all(stmt)
            return [_row_to_note_model(row) for row in rows]
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_all: {e}")
            raise

    async def get_prompt_notes(
        self,
        *,
        read_policy: NoteReadPolicy,
    ) -> list[NoteModel]:
        """Retrieves only regular notes that should be included in prompts (excludes skills)."""
        try:
            stmt = (
                select(*_NOTE_COLUMNS)
                .where(notes_table.c.include_in_prompt.is_(True))
                .where(notes_table.c.is_skill.is_(False))
                .order_by(notes_table.c.title)
            )
            stmt = self._apply_read_policy(stmt, read_policy)
            rows = await self._db.fetch_all(stmt)
            return [_row_to_note_model(row) for row in rows]
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_prompt_notes: {e}")
            raise

    async def get_excluded_notes_titles(
        self,
        *,
        read_policy: NoteReadPolicy,
    ) -> list[str]:
        """Titles of prompt-excluded notes, for the "Other available notes" line.

        Memory topic notes are left out for every reader. Their pointers live
        inside the capped core note's derived index, so the memory contribution
        to a rendered prompt is exactly the core note and nothing grows with
        the number of topics -- which is what makes the core note's cap a
        statement about the prompt. They stay reachable by title through
        ``get_note``, by search, and in the ``list_notes`` tool's output.
        """
        try:
            stmt = (
                select(notes_table.c.title)
                .where(notes_table.c.include_in_prompt.is_(False))
                .where(notes_table.c.is_skill.is_(False))
                .where(~self._labels_superset_condition([MEMORY_LABEL]))
                .order_by(notes_table.c.title)
            )
            stmt = self._apply_read_policy(stmt, read_policy)
            rows = await self._db.fetch_all(stmt)
            return [row["title"] for row in rows]
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_excluded_notes_titles: {e}")
            raise

    async def get_skills(
        self,
        *,
        read_policy: NoteReadPolicy,
    ) -> list[NoteModel]:
        """Retrieves notes that are skills, for building the skill catalog."""
        try:
            stmt = (
                select(*_NOTE_COLUMNS)
                .where(notes_table.c.is_skill.is_(True))
                .order_by(notes_table.c.skill_name)
            )
            stmt = self._apply_read_policy(stmt, read_policy)
            rows = await self._db.fetch_all(stmt)
            return [_row_to_note_model(row) for row in rows]
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_skills: {e}")
            raise

    async def get_by_id(
        self,
        note_id: int,
        *,
        read_policy: NoteReadPolicy,
    ) -> NoteRow | None:
        """Retrieves a note by its ID.

        Args:
            note_id: The ID of the note to retrieve
            read_policy: Confinement the read runs under; a row the policy does
                not admit reads as absent.

        Returns:
            NoteRow or None if not found/not accessible
        """
        query = select(notes_table).where(notes_table.c.id == note_id)
        query = self._apply_read_policy(query, read_policy)
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
                provenance_metadata=row.get("provenance_metadata_json"),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        return None

    async def get_memory_topic_notes(self) -> list[MemoryTopicNote]:
        """Every memory topic note, most recently changed first.

        No read policy: the label *is* the confinement here. A memory topic
        note is by definition inside the curator's read scope, and the caller
        is the review task assembling the curator's request rather than a tool
        resolving a title a model named.

        The core note is excluded: it reaches the curator through the notes
        context provider, and repeating it in the request would spend the
        review's input budget twice on the same text.
        """
        core_note_id = await self._db.memory_store.get_core_note_id()
        stmt = (
            select(
                notes_table.c.title,
                notes_table.c.content,
                notes_table.c.updated_at,
            )
            .where(self._labels_superset_condition([MEMORY_LABEL]))
            .order_by(notes_table.c.updated_at.desc(), notes_table.c.title)
        )
        if core_note_id is not None:
            stmt = stmt.where(notes_table.c.id != core_note_id)
        rows = await self._db.fetch_all(stmt)
        return [
            MemoryTopicNote(
                title=row["title"],
                content=row["content"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def get_by_title(
        self,
        title: str,
        *,
        read_policy: NoteReadPolicy,
    ) -> NoteModel | None:
        """Retrieves a specific note by its title."""
        try:
            stmt = select(*_NOTE_COLUMNS).where(notes_table.c.title == title)
            stmt = self._apply_read_policy(stmt, read_policy)
            row = await self._db.fetch_one(stmt)
            if row:
                return _row_to_note_model(row)
            return None
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_by_title({title}): {e}")
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
        additional_visibility_labels: list[str] | None = None,
        # ast-grep-ignore: no-dict-any - provenance metadata stores compact runtime taint JSON
        provenance_metadata: Mapping[str, object] | None = None,
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

        async def _write(txn: DatabaseTransaction) -> str:
            """Apply the policy preflight, the upsert and the index enqueue as one unit.

            The see-before-overwrite read and the write that depends on it
            must not be separated, and a note committed without its indexing
            task enqueued would never become searchable.
            """
            now = datetime.now(UTC)
            note_content = content

            existing_note = await txn.notes.get_by_title(
                title, read_policy=NoteReadPolicy.UNRESTRICTED
            )

            # See-before-overwrite: a restricted profile may not overwrite a note it
            # cannot see. Skipped when the policy carries no grants (admin bypass).
            if existing_note is not None and write_policy.visibility_grants is not None:
                visible_existing = await txn.notes.get_by_title(
                    title, read_policy=write_policy.see_before_overwrite_read_policy()
                )
                if visible_existing is None:
                    raise NoteWritePolicyError(
                        f"Cannot modify note '{title}' - insufficient visibility permissions."
                    )

            if append and existing_note:
                note_content = existing_note.content + "\n" + content

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
                existing_labels=existing_note.visibility_labels
                if existing_note
                else [],
            )
            if additional_visibility_labels:
                visibility_labels_to_use = write_policy.resolve_labels(
                    is_new_note=existing_note is None,
                    requested_labels=_merge_unique_labels(
                        visibility_labels_to_use, additional_visibility_labels
                    ),
                    existing_labels=existing_note.visibility_labels
                    if existing_note
                    else [],
                )

            if provenance_metadata is None and existing_note:
                provenance_metadata_to_use = existing_note.provenance_metadata
            else:
                provenance_metadata_to_use = provenance_metadata

            # Serialize to JSON strings
            attachment_ids_json = json.dumps(attachment_ids_to_use)
            visibility_labels_json = json.dumps(visibility_labels_to_use)

            # Detect skill metadata from frontmatter at write time
            is_skill, skill_name, skill_description = _detect_skill_metadata(
                note_content
            )

            existing_memory_labels = (
                existing_note.visibility_labels if existing_note else []
            )
            memory_write = is_memory_write(
                visibility_labels_to_use, existing_memory_labels
            )
            if memory_write:
                await self._enforce_memory_write(
                    txn,
                    lookup_title=title,
                    title=title,
                    content=note_content,
                    include_in_prompt=include_in_prompt,
                    resolved_labels=visibility_labels_to_use,
                    existing_labels=existing_memory_labels,
                    provenance_metadata=provenance_metadata_to_use,
                    now=now,
                )

            if txn.dialect_name == "postgresql":

                def _build_postgres_upsert() -> tuple[
                    Any, sa.ColumnElement[bool] | None
                ]:
                    stmt = pg_insert(notes_table).values(
                        title=title,
                        content=note_content,
                        include_in_prompt=include_in_prompt,
                        attachment_ids=attachment_ids_json,
                        visibility_labels=visibility_labels_json,
                        is_skill=is_skill,
                        skill_name=skill_name,
                        skill_description=skill_description,
                        provenance_metadata_json=provenance_metadata_to_use,
                        created_at=now,
                        updated_at=now,
                    )
                    update_dict = {
                        "content": stmt.excluded.content,
                        "include_in_prompt": stmt.excluded.include_in_prompt,
                        "attachment_ids": stmt.excluded.attachment_ids,
                        "visibility_labels": stmt.excluded.visibility_labels,
                        "is_skill": stmt.excluded.is_skill,
                        "skill_name": stmt.excluded.skill_name,
                        "skill_description": stmt.excluded.skill_description,
                        "provenance_metadata_json": stmt.excluded.provenance_metadata_json,
                        "updated_at": stmt.excluded.updated_at,
                    }
                    writable = self._writable_under_policy_condition(write_policy)
                    return (
                        stmt.on_conflict_do_update(
                            index_elements=["title"],
                            set_=update_dict,
                            where=writable,
                        ),
                        writable,
                    )

                def _ensure_write_allowed(
                    writable: sa.ColumnElement[bool] | None, rowcount: int
                ) -> None:
                    if writable is not None and rowcount == 0:
                        raise NoteWritePolicyError(
                            f"Cannot modify note '{title}' - a concurrently written "
                            "note with this title is outside the active profile's "
                            "write policy."
                        )

                # Use PostgreSQL's ON CONFLICT DO UPDATE for atomic upsert
                try:
                    stmt, writable = _build_postgres_upsert()
                    # Use execute_with_retry as commit is handled by context manager
                    result = await txn.execute(stmt)
                    _ensure_write_allowed(writable, result.rowcount)
                    self._logger.info(
                        f"Successfully added/updated note: {title} (using ON CONFLICT)"
                    )

                    # Enqueue indexing task
                    await self._enqueue_indexing_task(txn, title)
                except SQLAlchemyError as e:
                    self._logger.exception(
                        f"PostgreSQL error in add_or_update({title}): {e}"
                    )
                    raise
                if memory_write:
                    await self.refresh_core_memory_index(txn, now=now)
                return "Success"

            else:
                # Fallback for SQLite and other dialects: Try INSERT, then UPDATE on IntegrityError.
                insert_stmt = insert(notes_table).values(
                    title=title,
                    content=note_content,
                    include_in_prompt=include_in_prompt,
                    attachment_ids=attachment_ids_json,
                    visibility_labels=visibility_labels_json,
                    is_skill=is_skill,
                    skill_name=skill_name,
                    skill_description=skill_description,
                    provenance_metadata_json=provenance_metadata_to_use,
                    created_at=now,
                    updated_at=now,
                )
                try:
                    # Attempt INSERT first
                    await txn.execute(insert_stmt)
                    self._logger.info(f"Inserted new note: {title} (SQLite fallback)")

                    # Enqueue indexing task
                    await self._enqueue_indexing_task(txn, title)
                    if memory_write:
                        await self.refresh_core_memory_index(txn, now=now)
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
                                content=note_content,
                                include_in_prompt=include_in_prompt,
                                attachment_ids=attachment_ids_json,
                                visibility_labels=visibility_labels_json,
                                is_skill=is_skill,
                                skill_name=skill_name,
                                skill_description=skill_description,
                                provenance_metadata_json=provenance_metadata_to_use,
                                updated_at=now,
                            )
                        )
                        # Re-assert the write policy atomically with the update (the
                        # preflight cannot see a row another transaction inserted in
                        # the meantime); see the ON CONFLICT WHERE in the pg branch.
                        writable = self._writable_under_policy_condition(write_policy)
                        if writable is not None:
                            update_stmt = update_stmt.where(writable)
                        # Execute update within the same transaction context
                        result = await txn.execute(update_stmt)
                        if result.rowcount == 0:
                            if writable is not None:
                                raise NoteWritePolicyError(
                                    f"Cannot modify note '{title}' - a concurrently "
                                    "written note with this title is outside the "
                                    "active profile's write policy."
                                ) from e
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
                        await self._enqueue_indexing_task(txn, title)
                        if memory_write:
                            await self.refresh_core_memory_index(txn, now=now)
                        return "Success"
                    else:
                        # Re-raise other SQLAlchemy errors
                        self._logger.exception(
                            f"Database error during INSERT in add_or_update({title}) (SQLite fallback): {e}"
                        )
                        raise e

        return await self._db.atomic(_write)

    async def _enforce_memory_write(
        self,
        txn: DatabaseTransaction,
        *,
        lookup_title: str,
        title: str,
        content: str,
        include_in_prompt: bool,
        resolved_labels: list[str],
        existing_labels: list[str],
        # ast-grep-ignore: no-dict-any - provenance metadata stores compact runtime taint JSON
        provenance_metadata: Mapping[str, object] | None,
        now: datetime,
    ) -> None:
        """Apply the memory-store invariants and bump the store revision.

        Runs inside the caller's write transaction, so a write that violates an
        invariant rolls back with everything else in that unit of work, and the
        revision a concurrent proposal was computed against cannot move between
        the check and the write.

        Args:
            lookup_title: The title the note currently has, which is how its id
                is found — the core note is identified by id, so a rename of it
                must still resolve to the core note.
            title: The title the note will have after this write.
        """
        existing_note_id = await txn.fetch_value(
            select(notes_table.c.id).where(notes_table.c.title == lookup_title)
        )
        await enforce_memory_invariants(
            txn,
            limits=self._db.memory_limits,
            title=title,
            content=content,
            include_in_prompt=include_in_prompt,
            resolved_labels=resolved_labels,
            existing_note_id=existing_note_id,
            existing_labels=existing_labels,
            provenance_metadata=provenance_metadata,
            now=now,
        )
        await txn.memory_store.bump_revision()

    async def refresh_core_memory_index(
        self, txn: DatabaseTransaction, *, now: datetime
    ) -> bool:
        """Regenerate the core memory note's derived topic index.

        Runs after every write to any memory note -- the core note included,
        renames and deletions included -- inside the same transaction, so a
        pointer never outlives its topic or its title and no writer has to
        remember to update it. Applied to the stored content rather than to the
        submitted content, so a hand edit to the index section through the
        notes UI is overwritten by the regenerated one.

        This is itself a write to the core note, made as a plain UPDATE rather
        than through :meth:`add_or_update`, so it neither recurses nor bumps
        the store revision a second time for one logical change.

        Returns:
            Whether the core note's content changed.

        Raises:
            MemoryWriteError: if the regenerated core note would exceed its
                cap, which means the author's part leaves no room for the
                index.
        """
        limits = self._db.memory_limits
        core_note_id = await txn.memory_store.get_core_note_id()
        if core_note_id is None:
            return False

        core_row = await txn.fetch_one(
            select(notes_table.c.title, notes_table.c.content).where(
                notes_table.c.id == core_note_id
            )
        )
        if core_row is None:
            return False
        core_content: str = core_row["content"]

        topic_rows = await txn.fetch_all(
            select(notes_table.c.title, notes_table.c.updated_at)
            .where(notes_table.c.id != core_note_id)
            .where(self._labels_superset_condition([MEMORY_LABEL]))
        )
        topics = [
            TopicIndexEntry(title=row["title"], last_changed=row["updated_at"] or now)
            for row in topic_rows
        ]
        regenerated = regenerate_topic_index(
            core_content, topics, max_chars=limits.topic_index_max_chars
        )
        if regenerated == core_content:
            return False
        if len(regenerated) > limits.core_note_max_chars:
            raise MemoryWriteError(
                f"The core memory note would be {len(regenerated)} characters "
                f"with its regenerated topic index, over its "
                f"{limits.core_note_max_chars}-character limit. Condense its "
                "entries, or move detail into a memory topic note."
            )

        await txn.execute(
            update(notes_table)
            .where(notes_table.c.id == core_note_id)
            .values(content=regenerated, updated_at=now)
        )
        await self._enqueue_indexing_task(txn, core_row["title"])
        return True

    async def delete(self, title: str) -> bool:
        """Deletes a note by title.

        Raises:
            MemoryWriteError: if the note is the core memory note, which must
                always exist. Clearing its contents is an ordinary edit.
        """

        async def _delete(txn: DatabaseTransaction) -> bool:
            """Read the note's memory status and delete it as one unit."""
            row = await txn.fetch_one(
                select(notes_table.c.id, notes_table.c.visibility_labels).where(
                    notes_table.c.title == title
                )
            )
            if row is None:
                self._logger.warning(f"Note not found for deletion: {title}")
                return False

            is_memory_note = MEMORY_LABEL in _parse_json_list(row["visibility_labels"])
            if is_memory_note:
                core_note_id = await txn.memory_store.get_core_note_id()
                if core_note_id == row["id"]:
                    raise MemoryWriteError(
                        f"'{title}' is the core memory note and cannot be "
                        "deleted; exactly one always-loaded memory note must "
                        "exist. Clear its contents instead."
                    )

            result = await txn.execute(
                delete(notes_table).where(notes_table.c.id == row["id"])
            )
            if result.rowcount == 0:
                self._logger.warning(f"Note not found for deletion: {title}")
                return False
            if is_memory_note:
                await txn.memory_store.bump_revision()
                await self.refresh_core_memory_index(txn, now=datetime.now(UTC))
            self._logger.info(f"Deleted note: {title}")
            return True

        try:
            return await self._db.atomic(_delete)
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in delete({title}): {e}")
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
        # ast-grep-ignore: no-dict-any - provenance metadata stores compact runtime taint JSON
        provenance_metadata: Mapping[str, object] | None = None,
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
            return await self._rename_and_update(
                original_title,
                new_title,
                content,
                include_in_prompt,
                attachment_ids,
                visibility_labels,
                write_policy,
                provenance_metadata,
            )
        except (NoteNotFoundError, DuplicateNoteError, NoteWritePolicyError):
            raise
        except SQLAlchemyError as e:
            self._logger.exception(
                f"Database error in rename_and_update({original_title} -> {new_title}): {e}"
            )
            raise

    async def _rename_and_update(
        self,
        original_title: str,
        new_title: str,
        content: str,
        include_in_prompt: bool,
        attachment_ids: list[str] | None,
        visibility_labels: list[str] | None,
        write_policy: NoteWritePolicy,
        provenance_metadata: Mapping[str, object] | None,
    ) -> str:
        existing_note = await self.get_by_title(
            original_title, read_policy=NoteReadPolicy.UNRESTRICTED
        )
        if not existing_note:
            raise NoteNotFoundError(
                f"Cannot rename because note '{original_title}' was not found"
            )

        if write_policy.visibility_grants is not None:
            visible_existing = await self.get_by_title(
                original_title,
                read_policy=write_policy.see_before_overwrite_read_policy(),
            )
            if visible_existing is None:
                raise NoteWritePolicyError(
                    f"Cannot modify note '{original_title}' - insufficient "
                    "visibility permissions."
                )

        if new_title != original_title:
            conflicting_note = await self.get_by_title(
                new_title, read_policy=NoteReadPolicy.UNRESTRICTED
            )
            if conflicting_note:
                raise DuplicateNoteError(
                    f"A note with title '{new_title}' already exists"
                )

        attachment_ids_to_use = (
            existing_note.attachment_ids if attachment_ids is None else attachment_ids
        )
        visibility_labels_to_use = write_policy.resolve_labels(
            is_new_note=False,
            requested_labels=visibility_labels,
            existing_labels=existing_note.visibility_labels,
        )
        provenance_metadata_to_use = (
            existing_note.provenance_metadata
            if provenance_metadata is None
            else provenance_metadata
        )
        attachment_ids_json = json.dumps(attachment_ids_to_use)
        visibility_labels_json = json.dumps(visibility_labels_to_use)
        is_skill, skill_name, skill_description = _detect_skill_metadata(content)

        memory_write = is_memory_write(
            visibility_labels_to_use, existing_note.visibility_labels
        )

        async def _rename(txn: DatabaseTransaction) -> str:
            """Update the note and enqueue its indexing task as one unit."""
            if memory_write:
                await self._enforce_memory_write(
                    txn,
                    lookup_title=original_title,
                    title=new_title,
                    content=content,
                    include_in_prompt=include_in_prompt,
                    resolved_labels=visibility_labels_to_use,
                    existing_labels=existing_note.visibility_labels,
                    provenance_metadata=provenance_metadata_to_use,
                    now=datetime.now(UTC),
                )
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
                    provenance_metadata_json=provenance_metadata_to_use,
                    updated_at=func.now(),
                )
            )
            result = await txn.execute(stmt)
            if result.rowcount == 0:
                raise NoteNotFoundError(
                    f"Note '{original_title}' not found (may have been deleted)"
                )
            self._logger.info(f"Renamed note from '{original_title}' to '{new_title}'")
            await self._enqueue_indexing_task(txn, new_title)
            if memory_write:
                await self.refresh_core_memory_index(txn, now=datetime.now(UTC))
            return "Success"

        return await self._db.atomic(_rename)

    async def _enqueue_indexing_task(self, db: DatabaseExecutor, title: str) -> None:
        """
        Helper function to enqueue an indexing task for a note.

        Args:
            db: The executor the note was written through, so the enqueue joins
                the same unit of work.
            title: Title of the note to index
        """
        try:
            # Fetch the note to get its ID
            note_stmt = select(notes_table.c.id).where(notes_table.c.title == title)
            note_row = await db.fetch_one(note_stmt)
            if note_row:
                # Use UUID to ensure unique task IDs for re-indexing
                await db.tasks.enqueue(
                    task_id=f"index_note_{note_row['id']}_{uuid.uuid4()}",
                    task_type="index_note",
                    payload={"note_id": note_row["id"]},
                    priority=TaskPriority.BACKGROUND,
                )
                self._logger.info(
                    f"Enqueued indexing task for note ID {note_row['id']} (title: {title})"
                )
        except Exception as e:
            self._logger.error(
                f"Failed to enqueue indexing task for note '{title}': {e}"
            )
            # Don't fail the note operation if indexing task enqueueing fails
