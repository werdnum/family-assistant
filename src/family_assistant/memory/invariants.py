"""The memory-store invariants, enforced at the notes repository.

Every writer reaches the store through ``NotesRepository``: the curator, the
foreground ``add_or_update_note`` tool, and the web notes API, which writes
under ``NoteWritePolicy.UNCONSTRAINED`` and so never passes through any
tool-side policy. Enforcing here is what makes the guarantees in
docs/design/conversation-memory.md ("What v1 guarantees") statements about the
store rather than about one code path.

The invariants, for a note carrying the ``memory`` visibility label:

- exactly one always-loaded note, the core note, identified by id in
  ``memory_store.core_note_id``;
- ``include_in_prompt`` is true for the core note and false for every topic
  note, and the core note's flag cannot be turned off;
- the core note cannot be deleted, and cannot lose its ``memory`` label;
- content stays within the cap for its tier;
- the write's provenance is inside the trusted pole;
- no frontmatter, so a memory note can never become a skill;
- every memory write bumps the household store revision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa

from family_assistant.security.taint import TurnTaintState, is_admissible_for_reuse
from family_assistant.skills.frontmatter import parse_frontmatter
from family_assistant.storage.notes import notes_table

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from family_assistant.memory.limits import MemoryLimits
    from family_assistant.storage.database import DatabaseTransaction

MEMORY_LABEL = "memory"


@dataclass(frozen=True)
class CoreNote:
    """The core memory note this write is being held against.

    ``bootstrapped`` says the note did not exist until this call created it,
    which is the only situation in which the configured title identifies the
    core note. Afterwards the id is the only identity the core note has: a
    person may rename it, and a note that takes the vacated title is an
    ordinary topic note.
    """

    id: int
    bootstrapped: bool


class MemoryWriteError(Exception):
    """A write would leave the memory store in a shape v1 does not allow.

    The message is user-presentable: the notes UI shows it to the person who
    typed the note, the foreground tool returns it to the model, and the
    curator's apply path feeds it back as the reason an edit list was rejected.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MemoryStoreRevisionConflict(MemoryWriteError):
    """The store moved between reading it and writing against it."""


def is_memory_write(
    resolved_labels: Sequence[str],
    existing_labels: Sequence[str],
) -> bool:
    """Whether a write touches the memory store.

    True in three cases, all of which change what memory contains: writing a
    memory-labelled note, adding the label to an existing note, and removing it
    from one.
    """
    return MEMORY_LABEL in resolved_labels or MEMORY_LABEL in existing_labels


async def enforce_memory_invariants(
    txn: DatabaseTransaction,
    *,
    limits: MemoryLimits,
    title: str,
    content: str,
    include_in_prompt: bool,
    resolved_labels: Sequence[str],
    existing_note_id: int | None,
    existing_labels: Sequence[str],
    # ast-grep-ignore: no-dict-any - provenance metadata stores compact runtime taint JSON
    provenance_metadata: Mapping[str, object] | None,
    now: datetime,
) -> None:
    """Hold a pending memory-note write to the store invariants.

    Runs inside the caller's write transaction, after label resolution, so the
    checks and the write they guard cannot be separated. Bootstraps the core
    note when the store has none, which is why it takes the transaction rather
    than being a pure function.

    ``provenance_metadata`` of ``None`` passes the provenance floor
    deliberately: an unstamped write is one a signed-in household member made
    in the notes UI, which is the design's "a signed-in household member
    satisfies the provenance rule". A stamped write is refused when its
    recorded taint lies outside the trusted pole, whoever asked for it.

    Raises:
        MemoryWriteError: with a message for the writer to act on.
    """
    _check_provenance(provenance_metadata, title=title)

    core_note = await ensure_core_note(txn, limits=limits, now=now)
    writing_core = _writes_core_note(
        limits=limits,
        title=title,
        existing_note_id=existing_note_id,
        core_note=core_note,
    )

    if MEMORY_LABEL not in resolved_labels:
        # The note is leaving the store. Its size and prompt flag stop being
        # memory's business; that the core note stays in the store does not.
        if writing_core:
            raise MemoryWriteError(
                f"'{title}' is the always-loaded core memory note and must keep "
                "the 'memory' label."
            )
        return

    _check_no_frontmatter(content, title=title)

    if writing_core:
        if not include_in_prompt:
            raise MemoryWriteError(
                f"'{title}' is the always-loaded core memory note; it cannot be "
                "excluded from the prompt."
            )
        _check_cap(
            content,
            cap=limits.core_note_max_chars,
            title=title,
            advice=(
                "Condense it, or move detail into a memory topic note and leave "
                "a pointer here."
            ),
        )
        return

    if include_in_prompt:
        raise MemoryWriteError(
            "Only the core memory note is loaded into every prompt. Save "
            f"'{title}' as a memory topic note (include_in_prompt=false); it "
            "stays searchable and readable with get_note."
        )
    _check_cap(
        content,
        cap=limits.topic_note_max_chars,
        title=title,
        advice="Condense it, or split it into a further memory topic note.",
    )


async def ensure_core_note(
    txn: DatabaseTransaction,
    *,
    limits: MemoryLimits,
    now: datetime,
) -> CoreNote:
    """Return the core memory note, creating it if there is none.

    The core note is bootstrapped empty, always-loaded and memory-labelled, on
    the first memory write, so the store is never in the shape "some memory
    notes and no core note".

    Raises:
        MemoryWriteError: if a note already carries the core title but is not
            part of the memory store. Adopting it would pull a note somebody
            wrote for their own purposes into the curator's writable space.
    """
    core_note_id = await txn.memory_store.get_core_note_id()
    if core_note_id is not None:
        return CoreNote(id=core_note_id, bootstrapped=False)

    existing = await txn.fetch_one(
        sa.select(notes_table.c.id, notes_table.c.visibility_labels).where(
            notes_table.c.title == limits.core_note_title
        )
    )
    if existing is not None:
        # Reachable when the pointer was cleared out of band (the FK's ON
        # DELETE SET NULL) while a memory-labelled note kept the title.
        if MEMORY_LABEL not in _parse_labels(existing["visibility_labels"]):
            raise MemoryWriteError(
                f"A note titled '{limits.core_note_title}' already exists and is "
                "not part of memory. Rename it before the assistant can start "
                "keeping household memory."
            )
        await txn.memory_store.set_core_note_id(existing["id"])
        return CoreNote(id=int(existing["id"]), bootstrapped=True)

    new_id = await txn.notes.create_core_note(title=limits.core_note_title, now=now)
    await txn.memory_store.set_core_note_id(int(new_id))
    return CoreNote(id=int(new_id), bootstrapped=True)


def _writes_core_note(
    *,
    limits: MemoryLimits,
    title: str,
    existing_note_id: int | None,
    core_note: CoreNote,
) -> bool:
    """Whether this write targets the core note.

    By id, so that renaming the core note keeps it the core note -- and so
    that a note created later under the vacated title does not become a second
    always-loaded one. The title comparison covers the one case where the id
    cannot be known: the write that creates the core note is the same write
    ``ensure_core_note`` just bootstrapped it for, so the row it would update
    did not exist when the caller looked for it.
    """
    if existing_note_id is not None:
        return existing_note_id == core_note.id
    return core_note.bootstrapped and title == limits.core_note_title


def _check_provenance(
    # ast-grep-ignore: no-dict-any - provenance metadata stores compact runtime taint JSON
    provenance_metadata: Mapping[str, object] | None,
    *,
    title: str,
) -> None:
    if provenance_metadata is None:
        return
    state = TurnTaintState.from_metadata(provenance_metadata.get("taint_metadata"))
    if not is_admissible_for_reuse(state.max_tier):
        raise MemoryWriteError(
            f"Cannot write memory note '{title}': this turn has read content "
            "from outside the household, and memory holds nothing that "
            "originates outside it."
        )


def _check_no_frontmatter(content: str, *, title: str) -> None:
    frontmatter, _ = parse_frontmatter(content)
    if frontmatter is not None:
        raise MemoryWriteError(
            f"Memory note '{title}' cannot start with YAML frontmatter: memory "
            "notes are plain markdown and must not become skills."
        )


def _check_cap(content: str, *, cap: int, title: str, advice: str) -> None:
    if len(content) <= cap:
        return
    raise MemoryWriteError(
        f"Memory note '{title}' is {len(content)} characters, over its "
        f"{cap}-character limit. {advice}"
    )


def _parse_labels(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(label) for label in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(label) for label in parsed]
    return []
