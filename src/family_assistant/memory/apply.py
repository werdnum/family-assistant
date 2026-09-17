"""The one deterministic path by which anything is written to memory as entries.

See docs/design/conversation-memory.md, "The curator proposes edits; the apply
path enforces the invariants". The curator, a foreground "remember that ...",
and a foreground "forget that ..." all arrive here with the same thing: a short
list of entry-level edits and an evidence scope. This module validates the list
against the v1 invariants **all-or-nothing**, applies it in one short
transaction conditional on the store revision the writer read, and records every
change.

Whole-note writes -- the notes UI, ``add_or_update_note`` -- do not come through
here. They are held to the store invariants at the notes repository, which is
also where the core note's derived topic index is regenerated, so both kinds of
writer leave the store in the same shape.

**Entries and their references.** An entry is one markdown bullet; a bullet
whose continuation lines are indented is one entry. The curator writes the date,
the speaker and the kind of claim into the entry text itself, and this module
does not parse any of it. It does one thing to the text: where an added or
replaced entry does not already mention every message it cites, it appends a
stable suffix ``(refs: #123, #124)``, so the evidence survives in the plain
markdown a person reads and edits. A ``move`` carries its entry verbatim, refs
included, and cites nothing new.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from family_assistant.memory.edits import CITING_OPS, MemoryEdit, MemoryEditOp
from family_assistant.memory.index import strip_topic_index
from family_assistant.memory.invariants import (
    MEMORY_LABEL,
    MemoryStoreRevisionConflict,
    MemoryWriteError,
)
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.notes import (
    NoteReadPolicy,
    NoteWritePolicy,
    NoteWritePolicyError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from datetime import datetime

    import sqlalchemy as sa

    from family_assistant.memory.actor import MemoryActor
    from family_assistant.memory.edits import EvidenceScope
    from family_assistant.storage.database import Database, DatabaseTransaction

MEMORY_APPLY_WRITE_POLICY = NoteWritePolicy(
    visibility_grants=None,
    default_labels=[MEMORY_LABEL],
    required_labels=[MEMORY_LABEL],
    allowed_labels=None,
)
"""The confinement every note write from this module runs under.

The floor creates a new target as a memory note and makes the repository refuse
an existing note that is not one, so "every target note carries the ``memory``
label" is held where the write happens rather than only in the preflight below.
No ceiling: a deployment may add its own default labels to memory notes, and
this path preserves whatever an existing note already carries.
"""

_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>.*)$")


@dataclass(frozen=True)
class EditRejection:
    """Why one edit -- or the list as a whole -- was refused."""

    index: int | None
    """The edit's position in the proposed list; None when the list is at fault."""
    reason: str

    def describe(self) -> str:
        """A line a model can act on."""
        if self.index is None:
            return self.reason
        return f"edit {self.index + 1}: {self.reason}"


@dataclass(frozen=True)
class ApplyOutcome:
    """What an apply did, or why it did nothing."""

    applied: bool
    revision: int
    """The store revision after the apply, or the current one if nothing applied."""
    rejections: tuple[EditRejection, ...] = ()
    conflict: bool = False
    """The store moved under the proposal; the writer should re-read and retry.

    Kept apart from ``rejections`` because the remedy is different: nothing
    about the edits was wrong, so feeding the reasons back would teach the
    writer nothing.
    """
    batch_id: str | None = None
    changed_note_titles: tuple[str, ...] = ()


class MemoryEditsRejected(Exception):
    """Raised inside the transaction to roll the whole list back.

    Validation failures are *returned* to the caller rather than raised, because
    a model has to be told why in order to retry; but "nothing is applied" has
    to be enforced by a rollback, and returning normally from inside a
    transaction commits it. The transactional wrapper turns this back into the
    outcome it carries.
    """

    def __init__(self, outcome: ApplyOutcome) -> None:
        super().__init__("; ".join(r.describe() for r in outcome.rejections))
        self.outcome = outcome


async def apply_memory_edits(
    txn: DatabaseTransaction,
    edits: Sequence[MemoryEdit],
    *,
    evidence_scope: EvidenceScope,
    expected_revision: int,
    actor: MemoryActor,
    provenance_metadata: Mapping[str, object] | None,
    now: datetime,
    batch_id: str | None = None,
) -> ApplyOutcome:
    """Validate and apply an edit list inside ``txn``.

    Prefer :func:`apply_memory_edits_atomically`, which owns the transaction and
    converts a rollback back into an outcome. This form exists for a caller that
    already holds the transaction and wants the edits and its own work to commit
    together.

    Args:
        txn: The transaction the whole apply commits in.
        edits: The proposal, applied in order and all-or-nothing.
        evidence_scope: The message rows this writer may cite.
        expected_revision: The store revision the proposal was computed against.
        actor: Who is writing, as the change log records it.
        provenance_metadata: The writing turn's stamp, which the repository
            holds to the trusted pole.
        now: The apply's timestamp.
        batch_id: Groups this apply's change-log rows; generated when omitted.

    Returns:
        The outcome of a successful apply.

    Raises:
        MemoryEditsRejected: when anything was refused, so the transaction rolls
            back and nothing is applied.
    """
    limits = txn.memory_limits
    batch = batch_id or str(uuid.uuid4())

    if len(edits) > limits.max_edits_per_review:
        raise MemoryEditsRejected(
            ApplyOutcome(
                applied=False,
                revision=await txn.memory_store.get_revision(),
                rejections=(
                    EditRejection(
                        index=None,
                        reason=(
                            f"{len(edits)} edits were proposed, over the limit of "
                            f"{limits.max_edits_per_review} per review. Propose the "
                            "most important ones; the rest can wait for the next "
                            "review."
                        ),
                    ),
                ),
            )
        )

    await _check_evidence(txn, edits, evidence_scope=evidence_scope)

    try:
        await txn.memory_store.bump_revision(expected_revision=expected_revision)
    except MemoryStoreRevisionConflict as conflict:
        raise MemoryEditsRejected(
            ApplyOutcome(
                applied=False,
                revision=await txn.memory_store.get_revision(),
                conflict=True,
                rejections=(EditRejection(index=None, reason=str(conflict)),),
            )
        ) from conflict

    workspace = _Workspace(txn)
    records = [
        await _stage_edit(workspace, index, edit) for index, edit in enumerate(edits)
    ]
    await workspace.flush(provenance_metadata=provenance_metadata)

    for record in records:
        await txn.memory_change_log.add_edit(
            batch_id=batch,
            actor=actor,
            op=str(record.op),
            note_title=record.note_title,
            destination_note_title=record.destination_note_title,
            before_text=record.before_text,
            after_text=record.after_text,
            evidence_message_ids=record.evidence_message_ids,
            now=now,
        )

    return ApplyOutcome(
        applied=True,
        revision=await txn.memory_store.get_revision(),
        batch_id=batch,
        changed_note_titles=tuple(workspace.touched_titles),
    )


async def apply_memory_edits_atomically(
    db: Database,
    edits: Sequence[MemoryEdit],
    *,
    evidence_scope: EvidenceScope,
    expected_revision: int,
    actor: MemoryActor,
    provenance_metadata: Mapping[str, object] | None,
    now: datetime,
    batch_id: str | None = None,
    after_apply: Callable[[DatabaseTransaction], Awaitable[None]] | None = None,
) -> ApplyOutcome:
    """Apply an edit list in one short transaction, returning what happened.

    ``after_apply`` is the composition seam: it runs inside the same transaction
    once the edits are applied, so a caller can commit its own state -- the
    review watermark -- with them or not at all. A hook that raises rolls the
    edits back with it.

    No model call and no network round trip may happen inside this transaction,
    nor inside ``after_apply``.
    """

    async def _body(txn: DatabaseTransaction) -> ApplyOutcome:
        outcome = await apply_memory_edits(
            txn,
            edits,
            evidence_scope=evidence_scope,
            expected_revision=expected_revision,
            actor=actor,
            provenance_metadata=provenance_metadata,
            now=now,
            batch_id=batch_id,
        )
        if after_apply is not None:
            await after_apply(txn)
        return outcome

    try:
        return await db.atomic(_body)
    except MemoryEditsRejected as rejected:
        return rejected.outcome


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


async def _check_evidence(
    txn: DatabaseTransaction,
    edits: Sequence[MemoryEdit],
    *,
    evidence_scope: EvidenceScope,
) -> None:
    """Refuse the list if any edit cites a message outside the writer's scope.

    One query for every cited id, because the answer is the same for all of
    them: does this row exist, in this conversation, inside this stretch.

    Raises:
        MemoryEditsRejected: naming each edit and the ids it may not cite.
    """
    cited = {
        message_id
        for edit in edits
        if edit.op in CITING_OPS
        for message_id in edit.message_ids
    }
    if not cited:
        return

    condition: sa.ColumnElement[bool] = evidence_scope.condition(cited)
    rows = await txn.fetch_all(
        message_history_table
        .select()
        .with_only_columns(message_history_table.c.internal_id)
        .where(condition)
    )
    in_scope = {int(row["internal_id"]) for row in rows}

    rejections = tuple(
        EditRejection(
            index=index,
            reason=(
                "cites "
                + ", ".join(f"#{mid}" for mid in outside)
                + f", which {'is' if len(outside) == 1 else 'are'} not in "
                + evidence_scope.describe()
                + ". Cite only messages you were shown."
            ),
        )
        for index, edit in enumerate(edits)
        if (outside := [mid for mid in edit.message_ids if mid not in in_scope])
    )
    if rejections:
        raise MemoryEditsRejected(
            ApplyOutcome(
                applied=False,
                revision=await txn.memory_store.get_revision(),
                rejections=rejections,
            )
        )


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    """One block of a memory note: an entry, or prose that is not an entry."""

    is_entry: bool
    text: str

    def render(self) -> str:
        """The markdown this block contributes back to the note."""
        if not self.is_entry:
            return self.text
        lines = self.text.split("\n")
        rendered = [f"- {lines[0]}"]
        rendered.extend(f"  {line}" if line else "" for line in lines[1:])
        return "\n".join(rendered)


def normalise_entry(text: str) -> str:
    """The comparable form of an entry: no bullet marker, no stray whitespace.

    Applied to both the stored bullet and a writer's ``target_text``, so a
    writer may quote an entry with or without its marker and indentation.
    """
    lines = text.split("\n")
    normalised: list[str] = []
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if index == 0 and (match := _BULLET.match(raw)):
            stripped = match.group("text").strip()
        normalised.append(stripped)
    return "\n".join(normalised).strip()


def parse_entries(content: str) -> list[_Item]:
    """Split a memory note into entries and the prose between them."""
    items: list[_Item] = []
    entry_lines: list[str] | None = None
    prose_lines: list[str] = []
    pending_blanks: list[str] = []

    def _close_entry() -> None:
        nonlocal entry_lines
        if entry_lines is not None:
            items.append(
                _Item(is_entry=True, text=normalise_entry("\n".join(entry_lines)))
            )
            entry_lines = None

    def _close_prose() -> None:
        if prose_lines:
            items.append(_Item(is_entry=False, text="\n".join(prose_lines).strip("\n")))
            prose_lines.clear()

    for line in content.split("\n"):
        if not line.strip():
            pending_blanks.append(line)
            continue
        indented = line.startswith((" ", "\t"))
        if _BULLET.match(line) is not None and not indented:
            _close_entry()
            _close_prose()
            pending_blanks.clear()
            entry_lines = [line]
        elif entry_lines is not None and indented:
            entry_lines.extend(pending_blanks)
            pending_blanks.clear()
            entry_lines.append(line)
        else:
            _close_entry()
            prose_lines.extend(pending_blanks)
            pending_blanks.clear()
            prose_lines.append(line)
    _close_entry()
    _close_prose()
    return items


def render_entries(items: Sequence[_Item]) -> str:
    """Rebuild a memory note's markdown from its blocks."""
    return "\n".join(item.render() for item in items).strip()


def entry_texts(items: Sequence[_Item]) -> list[str]:
    """The normalised text of every entry, in order."""
    return [item.text for item in items if item.is_entry]


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ChangeRecord:
    """What the change log will say about one applied edit."""

    op: MemoryEditOp
    note_title: str
    destination_note_title: str | None
    before_text: str | None
    after_text: str | None
    evidence_message_ids: list[int]


@dataclass
class _WorkingNote:
    """One memory note as this apply is building it."""

    title: str
    items: list[_Item]
    exists: bool
    include_in_prompt: bool


@dataclass
class _Workspace:
    """The notes this apply touches, loaded once and written once."""

    txn: DatabaseTransaction
    notes: dict[str, _WorkingNote] = field(default_factory=dict)
    touched_titles: list[str] = field(default_factory=list)

    async def load(self, title: str, *, index: int) -> _WorkingNote:
        """Read a target note, creating a blank topic note if there is none.

        Raises:
            MemoryEditsRejected: if a note with this title exists and is not
                part of memory.
        """
        cached = self.notes.get(title)
        if cached is not None:
            return cached

        note = await self.txn.notes.get_by_title(
            title, read_policy=NoteReadPolicy.UNRESTRICTED
        )
        if note is not None and MEMORY_LABEL not in note.visibility_labels:
            await reject(
                self.txn,
                index,
                f"'{title}' is an existing note that is not part of memory. "
                "Memory edits may only touch memory notes; choose another title.",
            )
        working = _WorkingNote(
            title=title,
            items=parse_entries(strip_topic_index(note.content)) if note else [],
            exists=note is not None,
            # A new note is a topic note, with one exception: the very first
            # edit of a fresh store may name the core note, which the
            # repository requires to be always-loaded.
            include_in_prompt=(
                note.include_in_prompt
                if note
                else title == self.txn.memory_limits.core_note_title
            ),
        )
        self.notes[title] = working
        if title not in self.touched_titles:
            self.touched_titles.append(title)
        return working

    async def flush(
        self,
        *,
        provenance_metadata: Mapping[str, object] | None,
    ) -> None:
        """Write every touched note through the repository.

        The repository is where the caps, the always-loaded singleton, the
        provenance floor and the topic-index regeneration live, so a violation
        surfaces here as a rejection of the whole list.

        Raises:
            MemoryEditsRejected: carrying the repository's own message.
        """
        for title in self.touched_titles:
            working = self.notes[title]
            try:
                await self.txn.notes.add_or_update(
                    title=working.title,
                    content=render_entries(working.items),
                    include_in_prompt=working.include_in_prompt,
                    visibility_labels=None if working.exists else [MEMORY_LABEL],
                    write_policy=MEMORY_APPLY_WRITE_POLICY,
                    provenance_metadata=provenance_metadata,
                )
            except MemoryWriteError as error:
                await reject(self.txn, None, error.message)
            except NoteWritePolicyError as error:
                await reject(self.txn, None, str(error))


async def reject(txn: DatabaseTransaction, index: int | None, reason: str) -> None:
    """Refuse the whole list, rolling back whatever it had already applied.

    Raises:
        MemoryEditsRejected: always.
    """
    raise MemoryEditsRejected(
        ApplyOutcome(
            applied=False,
            revision=await txn.memory_store.get_revision(),
            rejections=(EditRejection(index=index, reason=reason),),
        )
    )


async def _stage_edit(
    workspace: _Workspace, index: int, edit: MemoryEdit
) -> _ChangeRecord:
    """Apply one edit to the in-memory working copy of its note(s).

    Raises:
        MemoryEditsRejected: if the edit's ``target_text`` matches no entry or
            more than one.
    """
    note = await workspace.load(edit.note_title, index=index)

    if edit.op is MemoryEditOp.ADD:
        entry = _with_refs(edit.entry or "", edit.message_ids)
        note.items.append(_Item(is_entry=True, text=entry))
        return _ChangeRecord(
            op=edit.op,
            note_title=edit.note_title,
            destination_note_title=None,
            before_text=None,
            after_text=entry,
            evidence_message_ids=list(edit.message_ids),
        )

    position = await _locate(workspace, note, index=index, edit=edit)
    before = note.items[position].text

    if edit.op is MemoryEditOp.REPLACE:
        entry = _with_refs(edit.entry or "", edit.message_ids)
        note.items[position] = _Item(is_entry=True, text=entry)
        return _ChangeRecord(
            op=edit.op,
            note_title=edit.note_title,
            destination_note_title=None,
            before_text=before,
            after_text=entry,
            evidence_message_ids=list(edit.message_ids),
        )

    note.items.pop(position)
    if edit.op is MemoryEditOp.REMOVE:
        return _ChangeRecord(
            op=edit.op,
            note_title=edit.note_title,
            destination_note_title=None,
            before_text=before,
            after_text=None,
            evidence_message_ids=list(edit.message_ids),
        )

    destination = await workspace.load(edit.destination_note_title or "", index=index)
    destination.items.append(_Item(is_entry=True, text=before))
    return _ChangeRecord(
        op=edit.op,
        note_title=edit.note_title,
        destination_note_title=destination.title,
        before_text=before,
        after_text=before,
        evidence_message_ids=[],
    )


async def _locate(
    workspace: _Workspace, note: _WorkingNote, *, index: int, edit: MemoryEdit
) -> int:
    """The position of the one entry ``target_text`` names.

    Raises:
        MemoryEditsRejected: on zero or several matches, quoting the note's
            current entries so the writer can quote one of them back exactly.
    """
    wanted = normalise_entry(edit.target_text or "")
    matches = [
        position
        for position, item in enumerate(note.items)
        if item.is_entry and item.text == wanted
    ]
    if len(matches) == 1:
        return matches[0]

    current = entry_texts(note.items)
    listing = (
        "\n".join(f"  - {text}" for text in current) if current else "  (no entries)"
    )
    trouble = "no entry matches" if not matches else f"{len(matches)} entries match"
    await reject(
        workspace.txn,
        index,
        f"{edit.op} on '{note.title}': {trouble} the given target_text. "
        f"The note's entries are:\n{listing}\n"
        "Quote one of them exactly.",
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _with_refs(entry: str, message_ids: Sequence[int]) -> str:
    """Make sure an entry's own text carries the messages it cites.

    Evidence has to survive in the markdown a person reads and edits, so an
    entry that does not already mention every cited id gets the stable suffix
    ``(refs: #123, #124)`` appended.
    """
    text = normalise_entry(entry)
    if not message_ids:
        return text
    if all(f"#{message_id}" in text for message_id in message_ids):
        return text
    refs = ", ".join(f"#{message_id}" for message_id in sorted(set(message_ids)))
    return f"{text} (refs: {refs})"
