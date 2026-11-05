"""The one place rows cross from one conversation into another.

Every path that carries content between a Home and its threads goes through
this module -- :func:`copy_across_boundary`, or :func:`move_turn_across_boundary`
for a turn the user moves -- so a path added later cannot skip a rule. It
applies three:

1. **Taint and profile.** Copied rows keep their taint exactly and are
   re-stamped to the destination's profile, because history windows are scoped
   by profile and an unstamped row would be invisible there.
2. **The context gate.** Home turns, their tool output included, may only enter
   a profile that already receives household context. Any other destination
   accepts request text only.
3. **Authorship.** Only text the user typed is stored as a user row. A request
   the assistant wrote is stored as machine-authored data -- an internal user
   row, as delegation results are -- so it never counts as user activity or as
   evidence of what the user said. A finished thread's echo is the thread
   assistant's own message, so it stays an assistant row.

A note one conversation's assistant posts into another of the user's
conversations (``post_to_conversation``) is an assistant request too: the
prompt of the turn it wakes there.

See docs/design/one-thread-chat-ux.md, "Moving content across the boundary",
and docs/design/cross-conversation-notes.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.llm.messages import UserMessage
from family_assistant.security.taint import (
    TurnTaintState,
    floor_machine_authored_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from family_assistant.llm.messages import MessageAttachmentMetadata
    from family_assistant.processing.types import ProcessingServiceConfig
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import DatabaseExecutor
    from family_assistant.storage.repositories.message_history import MovedTurnRows


class CrossBoundaryCopyRefusedError(Exception):
    """The destination's profile may not receive the content offered to it."""


@dataclass(frozen=True, slots=True)
class AssistantRequest:
    """A request the assistant wrote to open a thread.

    ``taint`` is the taint of the turn that wrote it, which the seed carries.
    """

    text: str
    taint: TaintMetadata | None
    attachments: Sequence[MessageAttachmentMetadata] = ()


@dataclass(frozen=True, slots=True)
class CopiedTurns:
    """Whole top-level turns of a source conversation, copied verbatim."""

    source_interface_type: str
    source_conversation_id: str
    turn_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThreadEcho:
    """A finished thread's final assistant message, echoed into its Home."""

    thread_conversation_id: str
    message_id: int


@dataclass(frozen=True, slots=True)
class BoundaryDestination:
    """Where content is going: a conversation and the profile that reads it."""

    interface_type: str
    conversation_id: str
    profile: ProcessingServiceConfig
    user_id: str


async def copy_across_boundary(
    db: DatabaseExecutor,
    *,
    destination: BoundaryDestination,
    payload: AssistantRequest | CopiedTurns | ThreadEcho,
    turn_id: str,
    now: datetime,
) -> list[int]:
    """Write ``payload`` into ``destination`` under the three rules.

    ``turn_id`` is the turn an assistant request or an echo is written under;
    copied turns each get a fresh turn of their own. Returns the new rows' ids
    in order.

    Raises:
        CrossBoundaryCopyRefusedError: ``payload`` carries conversation turns
            and the destination's profile does not receive household context.
    """
    match payload:
        case AssistantRequest():
            return [
                await _write_assistant_request(
                    db,
                    destination=destination,
                    request=payload,
                    turn_id=turn_id,
                    now=now,
                )
            ]
        case ThreadEcho():
            # Thread content entering Home, which receives household context
            # already: the context gate guards Home content leaving it.
            return [
                await db.message_history.copy_message_as_echo(
                    source_internal_id=payload.message_id,
                    destination_conversation_id=destination.conversation_id,
                    destination_profile_id=destination.profile.id,
                    echoed_from_conversation_id=payload.thread_conversation_id,
                    turn_id=turn_id,
                    timestamp=now,
                )
            ]
        case CopiedTurns():
            _require_household_context(destination)
            return await db.message_history.copy_turns(
                source_interface_type=payload.source_interface_type,
                source_conversation_id=payload.source_conversation_id,
                turn_ids=payload.turn_ids,
                destination_conversation_id=destination.conversation_id,
                destination_profile_id=destination.profile.id,
                timestamp=now,
            )


@dataclass(frozen=True, slots=True)
class MovedTurn:
    """One whole top-level turn of a source conversation, to be moved out of it."""

    source_interface_type: str
    source_conversation_id: str
    turn_id: str


MOVED_TURN_MARKER = "(The user moved an exchange from here into one of their threads.)"
"""What a moved turn leaves behind in its source.

Fixed text written by code, so it carries no taint; clients render the marker
from the thread it names rather than from this text.
"""


THREAD_START_MARKER = "(The user started a thread from here.)"
"""What a thread the user started leaves in Home.

Fixed text written by code, so it carries no taint; clients draw the thread's
card from the thread it names rather than from this text.
"""


async def move_turn_across_boundary(
    db: DatabaseExecutor,
    *,
    destination: BoundaryDestination,
    turn: MovedTurn,
    now: datetime,
) -> MovedTurnRows:
    """Move ``turn`` into ``destination`` under the three rules.

    The turn's rows keep their taint and are re-stamped to the destination's
    profile; the source keeps only a marker of where they went, so neither its
    readers nor its model see the moved content any more.

    Raises:
        CrossBoundaryCopyRefusedError: the destination's profile does not
            receive household context.
        LookupError: the source has no top-level rows of the turn.
    """
    _require_household_context(destination)
    return await db.message_history.move_turn(
        source_interface_type=turn.source_interface_type,
        source_conversation_id=turn.source_conversation_id,
        turn_id=turn.turn_id,
        destination_conversation_id=destination.conversation_id,
        destination_profile_id=destination.profile.id,
        marker_content=MOVED_TURN_MARKER,
        now=now,
    )


def _require_household_context(destination: BoundaryDestination) -> None:
    """The context gate: conversation turns enter only a profile that takes them."""
    if not destination.profile.receives_household_context:
        raise CrossBoundaryCopyRefusedError(
            f"Profile {destination.profile.id!r} does not receive household "
            "context, so conversation turns cannot be copied into it."
        )


async def _write_assistant_request(
    db: DatabaseExecutor,
    *,
    destination: BoundaryDestination,
    request: AssistantRequest,
    turn_id: str,
    now: datetime,
) -> int:
    # The writer's stamp splits what its turn introduced from what it inherited
    # from its own window, and the destination's window takes only what a row
    # introduced. Nothing of the writer's window is in the destination's, so
    # the request introduces all of it there: re-reading the stamp without its
    # origin does that. A turn with no taint tracker has introduced nothing
    # untrusted, which is how a delegation request with no recorded taint is
    # treated too.
    taint = floor_machine_authored_metadata(
        TurnTaintState.from_metadata(request.taint).to_metadata()
    )
    return await db.message_history.add_message(
        UserMessage(
            content=request.text,
            taint_metadata=taint,
            authorship_taint_metadata=taint,
        ),
        interface_type=destination.interface_type,
        conversation_id=destination.conversation_id,
        timestamp=now,
        turn_id=turn_id,
        processing_profile_id=destination.profile.id,
        user_id=destination.user_id,
        attachments=list(request.attachments) or None,
        is_internal=True,
    )
