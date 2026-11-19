"""Creating a thread: the one path every way of starting one goes through.

The assistant's ``start_thread`` and the user's own "New thread", send-as-thread
and "Continue in a thread" all create the link record and write what the thread
opens with here, so the seed always passes the boundary in
``family_assistant.threads.boundary``. What runs the first turn differs: a
thread the assistant starts is a delegation run, admitted by the target's
delegation policy, while a thread the user starts runs its first turn as the
user's own turn, like picking a profile.

See docs/design/one-thread-chat-ux.md, "Starting a thread".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.threads.boundary import (
    AssistantRequest,
    BoundaryDestination,
    CopiedTurns,
    copy_across_boundary,
)

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.processing.types import ProcessingServiceConfig
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import DatabaseExecutor


CONTINUE_CONTEXT_TURNS = 5
"""How many Home turns "Continue in a thread" carries: the anchor's and up to four before it.

Enough for the exchange the user is continuing and what led up to it, without
dragging a long stretch of unrelated Home chat -- tool output included -- into
the thread.
"""


TITLE_LENGTH = 60
"""How long a title taken from the text a thread starts with may run."""


def title_from_text(text: str) -> str:
    """A thread title from the text it starts with: its first line, shortened."""
    line = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
    if len(line) <= TITLE_LENGTH:
        return line
    cut = line[:TITLE_LENGTH].rsplit(" ", 1)[0]
    return f"{cut}…"


def new_thread_conversation_id(interface_type: str) -> str:
    """A fresh conversation id for a thread on ``interface_type``."""
    if interface_type == "web":
        return f"web_conv_{uuid.uuid4().hex}"
    return f"thread_{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class NewThread:
    """The link record of a thread about to be created.

    ``owner_user_id`` is canonical, as Homes are keyed; ``user_id`` is the id
    the owner's own rows carry.
    """

    conversation_id: str
    home_conversation_id: str
    interface_type: str
    owner_user_id: str
    user_id: str
    profile: ProcessingServiceConfig
    title: str
    title_taint: TaintMetadata | None
    anchor_message_id: int | None = None
    source_tool_call_id: str | None = None
    delegation_id: str | None = None


async def create_thread(
    db: DatabaseExecutor,
    *,
    thread: NewThread,
    seed: AssistantRequest | CopiedTurns | None,
    turn_id: str,
    now: datetime,
) -> list[int]:
    """Record ``thread`` and write ``seed`` into it across the boundary.

    An assistant request is recorded as the row that opened the thread, so
    clients can show it as "Assistant asked: ...". Run it in a transaction, so a
    seed the boundary refuses leaves no thread behind. Returns the seed's rows.

    Raises:
        CrossBoundaryCopyRefusedError: the seed carries conversation turns and
            the thread's profile does not receive household context.
    """
    await db.conversation_threads.create_thread(
        conversation_id=thread.conversation_id,
        home_conversation_id=thread.home_conversation_id,
        interface_type=thread.interface_type,
        owner_user_id=thread.owner_user_id,
        profile_id=thread.profile.id,
        source_tool_call_id=thread.source_tool_call_id,
        title=thread.title,
        title_taint=thread.title_taint,
        anchor_message_id=thread.anchor_message_id,
        delegation_id=thread.delegation_id,
        now=now,
    )
    if seed is None:
        return []
    seed_ids = await copy_across_boundary(
        db,
        destination=BoundaryDestination(
            interface_type=thread.interface_type,
            conversation_id=thread.conversation_id,
            profile=thread.profile,
            user_id=thread.user_id,
        ),
        payload=seed,
        turn_id=turn_id,
        now=now,
    )
    if isinstance(seed, AssistantRequest):
        [seed_message_id] = seed_ids
        await db.conversation_threads.set_seed_message(
            thread.conversation_id, seed_message_id, now
        )
    return seed_ids
