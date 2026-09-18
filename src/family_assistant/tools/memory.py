"""The tool every model-proposed change to household memory goes through.

One call carries the whole list, because validation is all-or-nothing: an edit
list is applied entirely or not at all, and a second call could not be part of
the first one's transaction. See docs/design/conversation-memory.md.

The result text is written for a model that has to retry. Where a
``target_text`` failed to match, the reason quotes the note's current entries
verbatim -- models paraphrase, and the difference between "close enough" and
"exactly" is the whole of the matching rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from family_assistant.memory.actor import MemoryActor, MemoryActorKind
from family_assistant.memory.edits import (
    CITING_OPS,
    EvidenceScope,
    MemoryEdit,
    MemoryEditList,
)
from family_assistant.tools.notes import note_provenance_from_taint
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from family_assistant.memory.apply import ApplyOutcome
    from family_assistant.memory.review_context import MemoryReviewContext
    from family_assistant.storage.database import DatabaseTransaction
    from family_assistant.tools.types import ToolExecutionContext

_REVIEW_EXHAUSTED = (
    "No memory edits were applied, and this review will not look at another "
    "proposal. A review gets one edit list and one retry after the reasons are "
    "fed back, and both have now been refused. Stop proposing edits and reply "
    "with a short note of what you would have kept, so the reason this review "
    "was given up on is on the record."
)
"""The reply to a third proposal, which the review budget refuses outright.

docs/design/conversation-memory.md allows one retry with the reasons fed back;
counting the attempts here rather than saying so in the prompt is what makes
that a bound rather than a request.
"""

MEMORY_WRITE_TOOL_NAMES: tuple[str, ...] = ("propose_memory_edits",)
"""The tools a profile that does not read the household's memory must not hold.

Writing memory requires reading it -- a profile that cannot see the existing
entries would be editing blind -- so these are withheld from a profile with
``memory_read`` off where every profile's tool policy is assembled
(``_build_profile_policy_engine``). Naming them here keeps the policy layer and
the refusal below saying the same thing about the same list.
"""

MEMORY_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "propose_memory_edits",
            "description": (
                "Change the household's long-term memory, one entry at a time. Memory is a small "
                "set of notes carrying the 'memory' label: one always-loaded core note of standing "
                "facts, plus topic notes per person, project or recurring theme. Use this tool when "
                "someone asks you to remember or forget something durable — a standing preference, "
                "a fact about a person or the household, a decision and its reason, a routine.\n\n"
                "This is NOT the tool for ordinary notes. `add_or_update_note` writes the user's own "
                "notes and documents; `propose_memory_edits` writes memory entries.\n\n"
                "Send the whole change as one call: the list is validated and applied together or "
                "not at all. Each edit is one of:\n"
                "- add: append a new entry to a note (creates the note as a memory topic note if it "
                "does not exist yet)\n"
                "- replace: swap one existing entry for new text\n"
                "- remove: delete one existing entry\n"
                "- move: relocate an entry verbatim to another memory note, which is how you make "
                "room in a full core note\n\n"
                "Rules the apply path enforces, so plan for them:\n"
                "- Every entry rests on a message, and the evidence is recorded for you. Leave "
                "`message_ids` out unless you were shown message ids to cite: when you are acting "
                "on what somebody just asked you, the message they asked in is cited "
                "automatically. Never invent an id. A move cites nothing either way: the entry "
                "keeps the references it already carries.\n"
                "- `target_text` must match exactly one existing entry in the named note, after "
                "leading '- ' and surrounding whitespace are ignored. If it matches none or several, "
                "the whole list is refused and the reply quotes the note's current entries — quote "
                "one of them back exactly.\n"
                "- Notes have size caps. If a write would overflow the core note, move the least "
                "standing entries to a topic note in the same call.\n"
                "- Write the date, who said it, and what kind of claim it is (a statement, a "
                "correction, an inference) into the entry text yourself. Message references are "
                "appended for you.\n\n"
                "Returns a string saying whether the list was applied, and if not, exactly why each "
                "edit was refused. A profile that does not read the household's memory cannot "
                "write it either, and every call is refused with that reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "The edits to apply together, in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["add", "replace", "remove", "move"],
                                    "description": "What to do to the note's entries.",
                                },
                                "note_title": {
                                    "type": "string",
                                    "description": "The memory note this edit applies to. A title that does not exist yet is created as a memory topic note.",
                                },
                                "entry": {
                                    "type": "string",
                                    "description": "The entry's new text, for 'add' and 'replace'. One entry, written as a sentence; include the date, the speaker, and whether it is a statement, a correction or an inference.",
                                },
                                "target_text": {
                                    "type": "string",
                                    "description": "The current text of the entry being changed, for 'replace', 'remove' and 'move'. Must match exactly one entry of the note.",
                                },
                                "destination_note_title": {
                                    "type": "string",
                                    "description": "For 'move' only: the memory note the entry is moved to.",
                                },
                                "message_ids": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": "The ids of the messages this edit rests on, for 'add', 'replace' and 'remove'. Supply them only when you were shown message ids; omit this field otherwise and the message being acted on is cited for you. Must be empty for 'move'.",
                                },
                            },
                            "required": ["op", "note_title"],
                        },
                    },
                },
                "required": ["edits"],
            },
        },
    },
]


async def propose_memory_edits_tool(
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - raw tool arguments, validated into MemoryEditList below
    edits: list[dict[str, Any]],
) -> ToolResult:
    """Validate and apply a proposed list of memory edits."""
    # Local import: the apply path reaches the notes repository, whose package
    # transitively imports the tools package, so a top-level import here would
    # be circular (``tools.notes`` does the same for the same reason).
    from family_assistant.memory.apply import (  # noqa: PLC0415
        apply_memory_edits_atomically,
    )

    if not exec_context.memory_read:
        return ToolResult(
            text=(
                "No memory edits were applied: this assistant profile does not "
                "read the household's memory, so it must not write to it. A "
                "profile that cannot see the existing entries would be editing "
                "blind -- duplicating what is already there, or replacing "
                "something it never read. Ask an operator to turn memory "
                "reading on for this profile."
            )
        )

    review = exec_context.memory_review
    if review is not None and review.proposals_exhausted:
        return ToolResult(text=_REVIEW_EXHAUSTED)

    try:
        proposal = MemoryEditList(edits=edits)  # type: ignore[arg-type] # validated from raw tool arguments
    except ValidationError as error:
        if review is not None:
            review.progress.refused_proposals += 1
        return ToolResult(
            text=f"No memory edits were applied. The proposal is malformed:\n{error}"
        )

    scope = _resolve_scope(exec_context)
    if scope is None:
        return ToolResult(
            text=(
                "No memory edits were applied: this turn has no message history to "
                "cite as evidence, and every memory entry must rest on a message."
            )
        )

    db_context = exec_context.db_context
    if review is None:
        bound = await _bind_turn_evidence(exec_context, proposal.edits)
        if isinstance(bound, str):
            return ToolResult(text=bound)
        proposal = MemoryEditList(edits=bound)

    now = (
        exec_context.clock.now()
        if exec_context.clock is not None
        else datetime.now(UTC)
    )

    after_apply: Callable[[DatabaseTransaction], Awaitable[None]] | None = None
    batch_id: str | None = None
    if review is None:
        expected_revision = await db_context.memory_store.get_revision()
    else:
        settled = review
        expected_revision = settled.expected_revision
        batch_id = settled.batch_id

        async def _advance(txn: DatabaseTransaction) -> None:
            """Commit the review's watermark with the edits it applied."""
            await settled.advance_watermark(txn, now=now)

        after_apply = _advance

    outcome = await apply_memory_edits_atomically(
        db_context,
        proposal.edits,
        # The apply path runs as this profile, not as a privileged memory
        # writer: a memory note this profile's own get_note hides is neither
        # quoted back to it nor edited by it.
        read_policy=exec_context.note_read_policy(),
        write_policy=exec_context.note_write_policy(),
        evidence_scope=scope,
        expected_revision=expected_revision,
        actor=_resolve_actor(exec_context),
        provenance_metadata=note_provenance_from_taint(exec_context),
        now=now,
        batch_id=batch_id,
        after_apply=after_apply,
    )
    if review is not None:
        _record_progress(review, outcome)
    return ToolResult(text=_render(outcome), data=_summarise(outcome))


def _record_progress(review: MemoryReviewContext, outcome: ApplyOutcome) -> None:
    """Tell the review task what its turn did, since the reply will not."""
    if outcome.applied:
        review.progress.applied_revision = outcome.revision
        return
    review.progress.refused_proposals += 1
    review.progress.conflicted = outcome.conflict


async def _bind_turn_evidence(
    exec_context: ToolExecutionContext, edits: Sequence[MemoryEdit]
) -> list[MemoryEdit] | str:
    """Cite the message the person asked in, for edits that cite nothing.

    A foreground assistant never sees a ``message_history.internal_id``: the
    turn reaches it as prose, and only the review transcript renders ids. So
    the citation is bound here from the turn the tool is running in rather than
    asked of a model that could only guess at it. An id the model *did* supply
    is left alone, and the apply path still holds it to the turn's scope.

    Returns the edits to apply, or the refusal to hand back when the turn has
    no user row to cite -- which is not a fact to paper over with an invented
    id, so it is reported as the failure it is.
    """

    def _needs_binding(edit: MemoryEdit) -> bool:
        return edit.op in CITING_OPS and not edit.message_ids

    if not any(_needs_binding(edit) for edit in edits):
        return list(edits)

    turn_id = exec_context.turn_id
    user_row = (
        await exec_context.db_context.message_history.get_user_row_by_turn_id(turn_id)
        if turn_id is not None
        else None
    )
    if user_row is None:
        return (
            "No memory edits were applied: this turn has no recorded request to "
            "cite as evidence, and every memory entry must rest on a message. "
            "Ask the person to say what they want remembered in a message of "
            "their own, and propose the edit from that."
        )
    internal_id = int(user_row["internal_id"])
    return [
        edit.model_copy(update={"message_ids": [internal_id]})
        if _needs_binding(edit)
        else edit
        for edit in edits
    ]


def _resolve_scope(exec_context: ToolExecutionContext) -> EvidenceScope | None:
    """The evidence this turn may cite: the review's stretch, or the turn itself."""
    if exec_context.memory_review is not None:
        return exec_context.memory_review.evidence_scope
    if exec_context.turn_id is None:
        return None
    return EvidenceScope.for_turn(
        interface_type=exec_context.interface_type,
        conversation_id=exec_context.conversation_id,
        turn_id=exec_context.turn_id,
    )


def _resolve_actor(exec_context: ToolExecutionContext) -> MemoryActor:
    """Who this apply is attributed to in the change log.

    A review is the only caller that runs under a review context, so that is
    what distinguishes a curator from a foreground "remember this".
    """
    kind = (
        MemoryActorKind.CURATOR
        if exec_context.memory_review is not None
        else MemoryActorKind.ASSISTANT
    )
    return MemoryActor(
        kind=kind,
        identity=exec_context.processing_profile_id,
        interface_type=exec_context.interface_type,
        conversation_id=exec_context.conversation_id,
    )


def _render(outcome: ApplyOutcome) -> str:
    """The whole result as the model sees it.

    Everything the model needs is here rather than in ``data``: a result that
    carries text shows the model only the text.
    """
    if outcome.applied:
        changed = ", ".join(f"'{title}'" for title in outcome.changed_note_titles)
        return (
            f"Applied {len(outcome.changed_note_titles)} memory note change(s) "
            f"({changed}). The memory store is now at revision {outcome.revision}."
        )
    if outcome.conflict:
        return (
            "No memory edits were applied. "
            + " ".join(rejection.reason for rejection in outcome.rejections)
            + f" The store is now at revision {outcome.revision}."
        )
    reasons = "\n".join(f"- {r.describe()}" for r in outcome.rejections)
    return f"No memory edits were applied. Every edit is refused together:\n{reasons}"


# ast-grep-ignore: no-dict-any - tool result payload has mixed value types
def _summarise(outcome: ApplyOutcome) -> dict[str, Any]:
    """The same outcome as structured data, for scripts and tests."""
    return {
        "applied": outcome.applied,
        "conflict": outcome.conflict,
        "revision": outcome.revision,
        "batch_id": outcome.batch_id,
        "changed_note_titles": list(outcome.changed_note_titles),
        "rejections": [
            {"index": rejection.index, "reason": rejection.reason}
            for rejection in outcome.rejections
        ],
    }
