"""The edit list a memory writer proposes, and the evidence it may cite.

See docs/design/conversation-memory.md, "The curator proposes edits; the apply
path enforces the invariants". A writer never rewrites a note: it proposes a
short list of entry-level edits, which :mod:`family_assistant.memory.apply`
validates and applies all-or-nothing.

The evidence scope is the other half of the contract. An add, replace or remove
must cite at least one message, and every cited message must lie inside the
scope its writer was given: the reviewed stretch for the curator, the current
turn for a foreground "remember this". The scope is a value rather than a
predicate so it can be carried on the execution context and rendered into an
error a model can act on.

*Who* fills in the citation differs by writer, which is why this model does not
require it. The curator is shown a transcript with an id against every message,
so it cites them itself and the apply path refuses a citing edit that names
none. A foreground assistant is shown no ids at all -- the turn's messages
reach it as prose -- so the tool binds the current turn's own user row before
the apply path sees the list, rather than the prompt asking the model for an id
it could only invent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from pydantic import BaseModel, Field, model_validator

from family_assistant.storage.message_history import message_history_table

if TYPE_CHECKING:
    from collections.abc import Iterable


class MemoryEditOp(StrEnum):
    """The four things a writer may do to a memory note's entries."""

    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"
    MOVE = "move"


CITING_OPS = frozenset({MemoryEditOp.ADD, MemoryEditOp.REPLACE, MemoryEditOp.REMOVE})
"""Operations that assert something, and so must rest on cited messages.

A move changes where an entry lives rather than what it says, so it carries the
entry's existing references and cites nothing new.
"""


class MemoryEdit(BaseModel):
    """One entry-level change to one memory note."""

    op: MemoryEditOp
    note_title: str = Field(min_length=1)
    entry: str | None = None
    """The entry's new text, for ``add`` and ``replace``."""
    target_text: str | None = None
    """The current text of the entry being changed, for everything but ``add``.

    Matched against the note's existing bullets after normalisation, and must
    match exactly one of them.
    """
    destination_note_title: str | None = None
    """Where a ``move`` puts the entry."""
    message_ids: list[int] = Field(default_factory=list)
    """``message_history.internal_id`` values this edit rests on.

    Left empty by a writer that was never shown any ids; the memory tool binds
    the current turn's user row in that case. A citing edit that still carries
    none by the time it reaches the apply path is refused there.
    """

    @model_validator(mode="after")
    def _check_fields_match_op(self) -> MemoryEdit:
        """Reject field combinations the apply path has no meaning for.

        Raises:
            ValueError: when a field is missing or present for the wrong op.
        """
        needs_entry = self.op in {MemoryEditOp.ADD, MemoryEditOp.REPLACE}
        if needs_entry and not (self.entry or "").strip():
            raise ValueError(f"'{self.op}' needs the new entry text in 'entry'.")
        if not needs_entry and self.entry is not None:
            raise ValueError(f"'{self.op}' does not take 'entry'.")

        needs_target = self.op is not MemoryEditOp.ADD
        if needs_target and not (self.target_text or "").strip():
            raise ValueError(
                f"'{self.op}' needs the current text of the entry in 'target_text'."
            )
        if not needs_target and self.target_text is not None:
            raise ValueError(f"'{self.op}' does not take 'target_text'.")

        if self.op is MemoryEditOp.MOVE:
            if not (self.destination_note_title or "").strip():
                raise ValueError("'move' needs 'destination_note_title'.")
            if self.message_ids:
                raise ValueError(
                    "'move' cites nothing: the entry keeps the references it "
                    "already carries."
                )
        elif self.destination_note_title is not None:
            raise ValueError(f"'{self.op}' does not take 'destination_note_title'.")
        return self


class MemoryEditList(BaseModel):
    """A proposal: the edits applied together or not at all."""

    edits: list[MemoryEdit] = Field(min_length=1)


@dataclass(frozen=True)
class EvidenceScope:
    """The message rows a writer is allowed to cite.

    One conversation, narrowed either to a single turn (the foreground case) or
    to a contiguous range of ``internal_id`` (the reviewed stretch). Both
    narrowings are always applied on top of the conversation, so a scope can
    never widen to a conversation a writer was not given.
    """

    interface_type: str
    conversation_id: str
    turn_id: str | None = None
    first_internal_id: int | None = None
    last_internal_id: int | None = None

    @classmethod
    def for_turn(
        cls, *, interface_type: str, conversation_id: str, turn_id: str
    ) -> EvidenceScope:
        """The current turn, which is what a foreground "remember this" cites."""
        return cls(
            interface_type=interface_type,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )

    @classmethod
    def for_stretch(
        cls,
        *,
        interface_type: str,
        conversation_id: str,
        first_internal_id: int,
        last_internal_id: int,
    ) -> EvidenceScope:
        """The unreviewed stretch of a conversation, which is what a review cites."""
        return cls(
            interface_type=interface_type,
            conversation_id=conversation_id,
            first_internal_id=first_internal_id,
            last_internal_id=last_internal_id,
        )

    def condition(self, message_ids: Iterable[int]) -> sa.ColumnElement[bool]:
        """SQL predicate selecting the cited rows that are inside this scope."""
        clauses = [
            message_history_table.c.internal_id.in_(list(message_ids)),
            message_history_table.c.interface_type == self.interface_type,
            message_history_table.c.conversation_id == self.conversation_id,
        ]
        if self.turn_id is not None:
            clauses.append(message_history_table.c.turn_id == self.turn_id)
        if self.first_internal_id is not None:
            clauses.append(
                message_history_table.c.internal_id >= self.first_internal_id
            )
        if self.last_internal_id is not None:
            clauses.append(message_history_table.c.internal_id <= self.last_internal_id)
        return sa.and_(*clauses)

    def describe(self) -> str:
        """A short phrase naming this scope, for a rejection a model has to act on."""
        where = f"conversation {self.interface_type}:{self.conversation_id}"
        if self.turn_id is not None:
            return f"the current turn of {where}"
        if self.first_internal_id is not None and self.last_internal_id is not None:
            return f"messages #{self.first_internal_id}-#{self.last_internal_id} of {where}"
        return where
