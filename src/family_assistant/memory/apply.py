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
writer leave the store in the same shape. A whole-note write regenerates the
index as part of itself; a batch from here regenerates it once, when every note
the batch touches has been written, so the list is judged on its final state.

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
from typing import TYPE_CHECKING, NoReturn

import sqlalchemy as sa

from family_assistant.memory.edits import CITING_OPS, MemoryEdit, MemoryEditOp
from family_assistant.memory.index import strip_topic_index
from family_assistant.memory.invariants import (
    MEMORY_LABEL,
    MemoryStoreRevisionConflict,
    MemoryWriteError,
)
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.notes import (
    NoteWritePolicy,
    NoteWritePolicyError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from family_assistant.memory.actor import MemoryActor
    from family_assistant.memory.edits import EvidenceScope
    from family_assistant.security.note_provenance import NoteProvenanceStamp
    from family_assistant.storage.database import Database, DatabaseTransaction
    from family_assistant.storage.repositories.notes import NoteReadPolicy


def memory_write_policy(caller: NoteWritePolicy) -> NoteWritePolicy:
    """The caller's own note confinement with the ``memory`` floor added.

    The apply path writes as the profile that called it, not as a privileged
    memory writer: whatever the caller may not overwrite through
    ``add_or_update_note`` it may not overwrite through an edit list either.
    The floor is what this path adds on top -- it creates a new target as a
    memory note and makes the repository refuse an existing note that is not
    one, so "every target note carries the ``memory`` label" is held where the
    write happens rather than only in the preflight.

    The floor has to be reachable to be a floor, so ``memory`` is unioned into
    the caller's grants and into its allowed-label ceiling where it has one; a
    caller that is *denied* the label keeps that denial, which is what refuses
    a profile that does not read memory at the repository as well as at the
    tool. No ceiling is invented where the caller has none: a deployment may
    add its own default labels to memory notes, and this path preserves
    whatever an existing note already carries.
    """
    return NoteWritePolicy(
        visibility_grants=(
            None
            if caller.visibility_grants is None
            else set(caller.visibility_grants) | {MEMORY_LABEL}
        ),
        default_labels=[MEMORY_LABEL],
        required_labels=_with_memory_label(caller.required_labels or []),
        allowed_labels=(
            None
            if caller.allowed_labels is None
            else _with_memory_label(caller.allowed_labels)
        ),
        denied_labels=caller.denied_labels,
    )


def _with_memory_label(labels: Sequence[str]) -> list[str]:
    """``labels`` with the ``memory`` label appended if it is not already there."""
    if MEMORY_LABEL in labels:
        return list(labels)
    return [*labels, MEMORY_LABEL]


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
    read_policy: NoteReadPolicy,
    write_policy: NoteWritePolicy,
    evidence_scope: EvidenceScope,
    expected_revision: int,
    actor: MemoryActor,
    provenance: NoteProvenanceStamp,
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
        read_policy: The calling profile's own note read confinement. Every
            target and move destination is resolved under it, so this path
            cannot show -- or edit -- a memory note ``get_note`` would hide.
        write_policy: The calling profile's own note write confinement, which
            this path writes under with the ``memory`` floor added (see
            :func:`memory_write_policy`).
        evidence_scope: The message rows this writer may cite.
        expected_revision: The store revision the proposal was computed against.
        actor: Who is writing, as the change log records it.
        provenance: The writing turn's stamp. The repository merges it with
            the stored tier of each note it rewrites, since untouched entries
            are retained, and holds the result to the reuse predicate.
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

    # Read once, before the compare-and-set below moves it. Every rejection
    # rolls the transaction back, so the revision a rejected outcome reports
    # must be the one that survives the rollback; reading it again after the
    # bump would hand the writer a number that was never committed, and a retry
    # against it would conflict for ever.
    revision_before = await txn.memory_store.get_revision()

    if len(edits) > limits.max_edits_per_review:
        raise MemoryEditsRejected(
            ApplyOutcome(
                applied=False,
                revision=revision_before,
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

    await _check_evidence(
        txn, edits, evidence_scope=evidence_scope, revision=revision_before
    )

    try:
        await txn.memory_store.bump_revision(expected_revision=expected_revision)
    except MemoryStoreRevisionConflict as conflict:
        raise MemoryEditsRejected(
            ApplyOutcome(
                applied=False,
                revision=revision_before,
                conflict=True,
                rejections=(EditRejection(index=None, reason=str(conflict)),),
            )
        ) from conflict

    workspace = _Workspace(
        txn,
        revision_before,
        read_policy=read_policy,
        write_policy=memory_write_policy(write_policy),
    )
    records = [
        await _stage_edit(workspace, index, edit) for index, edit in enumerate(edits)
    ]
    await workspace.flush(provenance=provenance, now=now)

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
    read_policy: NoteReadPolicy,
    write_policy: NoteWritePolicy,
    evidence_scope: EvidenceScope,
    expected_revision: int,
    actor: MemoryActor,
    provenance: NoteProvenanceStamp,
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
            read_policy=read_policy,
            write_policy=write_policy,
            evidence_scope=evidence_scope,
            expected_revision=expected_revision,
            actor=actor,
            provenance=provenance,
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
    revision: int,
) -> None:
    """Refuse the list unless every citing edit rests on messages in scope.

    Two ways to fail, both refused here so no writer can reach the store
    without evidence: a citing edit that names no message at all, and one that
    names a message outside the scope. One query for every cited id, because
    the answer is the same for all of them: does this row exist, in this
    conversation, inside this stretch.

    Raises:
        MemoryEditsRejected: naming each edit and what is wrong with its
            citations.
    """
    uncited = tuple(
        EditRejection(
            index=index,
            reason=(
                f"'{edit.op}' must cite at least one message in 'message_ids', "
                "and cites none. Cite a message you were shown."
            ),
        )
        for index, edit in enumerate(edits)
        if edit.op in CITING_OPS and not edit.message_ids
    )
    if uncited:
        raise MemoryEditsRejected(
            ApplyOutcome(applied=False, revision=revision, rejections=uncited)
        )

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
                revision=revision,
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
    revision: int
    """The store revision a rejection from this workspace reports."""
    read_policy: NoteReadPolicy
    """The calling profile's read confinement, which every target is resolved under."""
    write_policy: NoteWritePolicy
    """The calling profile's write confinement, with the ``memory`` floor added."""
    notes: dict[str, _WorkingNote] = field(default_factory=dict)
    touched_titles: list[str] = field(default_factory=list)

    async def load(self, title: str, *, index: int) -> _WorkingNote:
        """Read a target note, creating a blank topic note if there is none.

        Resolved under the caller's own read policy, so an edit list cannot
        reach a memory note ``get_note`` hides from the same profile -- neither
        to quote its entries back in a rejection nor to edit them. A row that
        exists but is not visible is refused in the same words as one that is
        visible and not a memory note: it is not a note this caller may edit,
        and the reply says nothing further about it.

        Raises:
            MemoryEditsRejected: if a note with this title exists and is not a
                memory note this caller may edit.
        """
        cached = self.notes.get(title)
        if cached is not None:
            return cached

        note = await self.txn.notes.get_by_title(title, read_policy=self.read_policy)
        if (note is None and await self._title_taken(title)) or (
            note is not None and MEMORY_LABEL not in note.visibility_labels
        ):
            reject(
                self.revision,
                index,
                f"'{title}' is not a memory note you can edit. Memory edits may "
                "only touch the memory notes this profile reads; choose another "
                "title.",
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

    async def _title_taken(self, title: str) -> bool:
        """Whether any row holds this title, visible to this caller or not.

        The existence question the read policy cannot answer, asked without
        selecting the row's contents: a title the caller cannot see is still a
        title it cannot create a note under, and refusing it here is what keeps
        an invisible note's entries out of the rejection message.
        """
        row_id = await self.txn.fetch_value(
            sa.select(notes_table.c.id).where(notes_table.c.title == title)
        )
        return row_id is not None

    async def flush(
        self,
        *,
        provenance: NoteProvenanceStamp,
        now: datetime,
    ) -> None:
        """Write every touched note through the repository.

        The repository is where the caps, the always-loaded singleton and the
        provenance floor live, so a violation surfaces here as a rejection of
        the whole list.

        The core note's derived topic index is regenerated once, after the last
        note is written, rather than per write: a list is validated on the state
        it leaves behind, and a half-applied batch can be over the core note's
        cap in a shape its final state never has -- adding an entry to a new
        topic note before moving the core entry that made room for it, say. Held
        per write, the same list would be applied or refused depending on the
        order its edits happen to be in.

        Raises:
            MemoryEditsRejected: carrying the repository's own message.
        """
        try:
            for title in self.touched_titles:
                working = self.notes[title]
                await self.txn.notes.add_or_update(
                    title=working.title,
                    content=render_entries(working.items),
                    include_in_prompt=working.include_in_prompt,
                    visibility_labels=None if working.exists else [MEMORY_LABEL],
                    write_policy=self.write_policy,
                    provenance=provenance,
                    refresh_core_index=False,
                )
            if self.touched_titles:
                await self.txn.notes.refresh_core_memory_index(self.txn, now=now)
        except MemoryWriteError as error:
            reject(self.revision, None, error.message)
        except NoteWritePolicyError as error:
            reject(self.revision, None, str(error))


def reject(revision: int, index: int | None, reason: str) -> NoReturn:
    """Refuse the whole list, rolling back whatever it had already applied.

    ``revision`` is the store revision as it stood before this apply touched
    it, which is what survives the rollback and what a retry must propose
    against.

    Raises:
        MemoryEditsRejected: always.
    """
    raise MemoryEditsRejected(
        ApplyOutcome(
            applied=False,
            revision=revision,
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
    reject(
        workspace.revision,
        index,
        f"{edit.op} on '{note.title}': {trouble} the given target_text. "
        f"The note's entries are:\n{listing}\n"
        "Quote one of them exactly.",
    )


def _mentions_reference(text: str, message_id: int) -> bool:
    """Whether ``text`` already cites exactly this message.

    Bounded on digits at both ends: a plain substring test would read ``#123``
    as a mention of ``#12`` and drop the suffix that carries the real evidence.
    """
    return re.search(rf"(?<!\d)#{message_id}(?!\d)", text) is not None


def _with_refs(entry: str, message_ids: Sequence[int]) -> str:
    """Make sure an entry's own text carries the messages it cites.

    Evidence has to survive in the markdown a person reads and edits, so an
    entry that does not already mention every cited id gets the stable suffix
    ``(refs: #123, #124)`` appended.
    """
    text = normalise_entry(entry)
    if not message_ids:
        return text
    if all(_mentions_reference(text, message_id) for message_id in message_ids):
        return text
    refs = ", ".join(f"#{message_id}" for message_id in sorted(set(message_ids)))
    return f"{text} (refs: {refs})"
