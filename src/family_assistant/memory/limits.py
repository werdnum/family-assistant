"""Bounds on the conversation-memory store.

The caps are the only size control in v1 (see docs/design/conversation-memory.md,
"Size management without a consolidation pass"): a write that would leave a
memory note over its ceiling is refused at the repository, and the writer is
told to condense or move detail to a topic note.

Counted in characters of note content rather than tokens: the check runs inside
the write transaction, where no tokenizer is available and a cheap, stable,
explainable number is worth more than an accurate one. The defaults are sized
so that the always-loaded core note is a small share of a prompt and no single
memory note exceeds what one review can read.
"""

from dataclasses import dataclass
from typing import ClassVar

DEFAULT_CORE_NOTE_TITLE = "Household Memory"


@dataclass(frozen=True)
class MemoryLimits:
    """The shape parameters of the memory store, in force for one database.

    ``core_note_title`` is not a limit, but it travels with them: it is the
    title the core note is bootstrapped under, and the repository needs both
    from the same object (see ``DatabaseExecutor.memory_limits``).
    """

    core_note_max_chars: int = 6000
    topic_note_max_chars: int = 12000
    topic_index_max_chars: int = 1500
    review_input_max_chars: int = 24000
    max_edits_per_review: int = 12
    core_note_title: str = DEFAULT_CORE_NOTE_TITLE

    DEFAULTS: ClassVar["MemoryLimits"]

    @property
    def review_transcript_max_chars(self) -> int:
        """The transcript's share of one review's input budget.

        Derived rather than configured: the split between the transcript and
        the memory entries shown beside it is a property of what a review is
        for, not a knob a deployment tunes. Two thirds to the transcript, so a
        long settled conversation still leaves room for the entries the curator
        is told to update rather than duplicate.
        """
        return max(1, self.review_input_max_chars * 2 // 3)


MemoryLimits.DEFAULTS = MemoryLimits()
