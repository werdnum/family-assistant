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
from family_assistant.memory.edits import EvidenceScope, MemoryEditList
from family_assistant.tools.notes import note_provenance_from_taint
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.memory.apply import ApplyOutcome
    from family_assistant.tools.types import ToolExecutionContext

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
                "- add, replace and remove must cite at least one message id in `message_ids`, and "
                "every cited message must be one you were shown in this turn or review. A move "
                "cites nothing: the entry keeps the references it already carries.\n"
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
                                    "description": "The message ids this edit rests on. Required for 'add', 'replace' and 'remove'; must be empty for 'move'.",
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

    try:
        proposal = MemoryEditList(edits=edits)  # type: ignore[arg-type] # validated from raw tool arguments
    except ValidationError as error:
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
    expected_revision = exec_context.memory_expected_revision
    if expected_revision is None:
        expected_revision = await db_context.memory_store.get_revision()

    now = (
        exec_context.clock.now()
        if exec_context.clock is not None
        else datetime.now(UTC)
    )
    outcome = await apply_memory_edits_atomically(
        db_context,
        proposal.edits,
        evidence_scope=scope,
        expected_revision=expected_revision,
        actor=_resolve_actor(exec_context),
        provenance_metadata=note_provenance_from_taint(exec_context),
        now=now,
    )
    return ToolResult(text=_render(outcome), data=_summarise(outcome))


def _resolve_scope(exec_context: ToolExecutionContext) -> EvidenceScope | None:
    """The evidence this turn may cite: the review's stretch, or the turn itself."""
    if exec_context.memory_evidence_scope is not None:
        return exec_context.memory_evidence_scope
    if exec_context.turn_id is None:
        return None
    return EvidenceScope.for_turn(
        interface_type=exec_context.interface_type,
        conversation_id=exec_context.conversation_id,
        turn_id=exec_context.turn_id,
    )


def _resolve_actor(exec_context: ToolExecutionContext) -> MemoryActor:
    """Who this apply is attributed to in the change log.

    A review is the only caller that supplies its own evidence scope, so that
    is what distinguishes a curator from a foreground "remember this".
    """
    kind = (
        MemoryActorKind.CURATOR
        if exec_context.memory_evidence_scope is not None
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
