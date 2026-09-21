"""What one memory review hands to the curator's turn, and gets back from it.

See docs/design/conversation-memory.md, "The curator proposes edits; the apply
path enforces the invariants". Three things have to travel from the review task
into ``propose_memory_edits`` and cannot be derived inside it:

- the **evidence scope**, the stretch of transcript the curator was shown, which
  is what the apply path checks a citation against;
- the **store revision** the review read before the model call, which is what
  makes an edit list proposed against a store a person has since changed fail
  rather than overwrite it;
- the **watermark** the review advances, which has to move in the same
  transaction as the edits or a crash between them either re-reviews a curated
  stretch or loses one.

They travel as one value rather than as loose context fields so a turn can never
carry half of them -- an evidence scope without the revision it was computed
against would apply an edit list that no compare-and-set guarded.

What comes back travels on :class:`MemoryReviewProgress`, the one mutable part.
The tool records there what it did, because the review's terminal outcome is
decided by what happened inside the turn and the turn itself only returns the
model's last reply. It is also where the retry budget is counted: the design
allows a rejected list to be retried **once**, and counting attempts here makes
that a property of the review rather than of an instruction the model may
ignore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.memory.edits import EvidenceScope
    from family_assistant.storage.database import DatabaseTransaction

MAX_PROPOSALS_PER_REVIEW = 2
"""A first proposal and one retry after the reasons are fed back.

docs/design/conversation-memory.md: "a rejected list is retried once with the
reasons fed back before the review is abandoned".
"""


@dataclass
class MemoryReviewProgress:
    """What the curator's turn has done to memory so far.

    Mutated by ``propose_memory_edits`` and read by the review task once the
    turn is over.
    """

    refused_proposals: int = 0
    """How many edit lists this review has had refused, for any reason."""
    conflicted: bool = False
    """The last refusal was a store-revision conflict rather than a bad edit."""
    applied_revision: int | None = None
    """The revision the store reached when a list was applied; None until then."""

    @property
    def applied(self) -> bool:
        """Whether this review committed an edit list."""
        return self.applied_revision is not None


@dataclass(frozen=True)
class MemoryReviewContext:
    """The review a curator turn is running for.

    Frozen except for :attr:`progress`: everything the turn is *given* is
    settled before the model call, and only what it *did* moves.
    """

    evidence_scope: EvidenceScope
    expected_revision: int
    batch_id: str
    interface_type: str
    conversation_id: str
    watermark_target: int
    """The chunk's last ``internal_id``, which a successful apply advances to."""
    progress: MemoryReviewProgress = field(default_factory=MemoryReviewProgress)

    @property
    def proposals_exhausted(self) -> bool:
        """Whether this review has spent its proposal and its one retry."""
        return self.progress.refused_proposals >= MAX_PROPOSALS_PER_REVIEW

    async def advance_watermark(
        self, txn: DatabaseTransaction, *, now: datetime
    ) -> None:
        """Move the conversation's watermark past this chunk inside ``txn``.

        Passed to the apply path as its ``after_apply`` hook, so the edits and
        the advance commit together or not at all.
        """
        await txn.memory_review.advance_watermark(
            interface_type=self.interface_type,
            conversation_id=self.conversation_id,
            last_reviewed_internal_id=self.watermark_target,
            now=now,
        )
