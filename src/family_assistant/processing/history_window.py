"""The prompt's history window, built from whole turns against a size budget.

A turn is the rows its own turn wrote; a row with no turn id (a proactive send,
a pinned data row) is a turn of its own. A turn is never split, so a window can
never start with a tool result whose call it lost, or keep an answer without the
request it answered. See docs/design/history-compaction.md.

Every path that needs the window -- the turn itself and the web producer's taint
computation -- goes through :class:`HistoryWindowLoader`, so they agree on which
rows are in the prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.llm.messages import (
    AssistantMessage,
    ErrorMessage,
    ImageUrlContentPart,
    LLMMessage,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence
    from datetime import datetime, timedelta

    from family_assistant.llm.messages import MessageWithMetadata
    from family_assistant.storage.database import Database

# What an inlined image counts for against the budget. Images are re-sent as
# bytes, which no character count measures; this is roughly what a provider
# charges for a typical photo, in characters of text.
IMAGE_CHAR_COST = 4_000

# The most rows one window read fetches before whole turns are completed. Far
# above any budget's worth of rows; it bounds the read of a conversation whose
# age cap admits months of activity.
WINDOW_ROW_LIMIT = 2_000


@dataclass(frozen=True, slots=True)
class HistoryLimits:
    """How much history a window may hold."""

    budget_chars: int
    min_turns: int
    max_age: timedelta


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One turn's rows, oldest first."""

    key: str
    rows: tuple[MessageWithMetadata, ...]

    @property
    def last_activity(self) -> datetime:
        return max(row.timestamp for row in self.rows)


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """The rendered history of one request.

    ``turns`` are the completed turns in the window, oldest first, and
    ``messages`` their rendering. ``active_messages`` renders the turn being
    run, which is always sent whole and is never part of ``turns``.
    """

    turns: tuple[HistoryTurn, ...]
    messages: list[LLMMessage]
    active_messages: list[LLMMessage]


def turn_key(row: MessageWithMetadata) -> str:
    return row.turn_id if row.turn_id is not None else f"row:{row.internal_id}"


def group_turns(rows: Sequence[MessageWithMetadata]) -> list[HistoryTurn]:
    """Rows grouped into turns, each turn placed where its first row was written."""
    by_key: dict[str, list[MessageWithMetadata]] = {}
    seen: set[str] = set()
    for row in sorted(rows, key=lambda row: (row.timestamp, int(row.internal_id))):
        if row.internal_id in seen:
            continue
        seen.add(row.internal_id)
        by_key.setdefault(turn_key(row), []).append(row)
    return [
        HistoryTurn(key=key, rows=tuple(turn_rows)) for key, turn_rows in by_key.items()
    ]


def _content_size(content: object) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        size = 0
        for part in content:
            if isinstance(part, TextContentPart):
                size += len(part.text)
            elif isinstance(part, ImageUrlContentPart):
                size += IMAGE_CHAR_COST
            else:
                size += len(json.dumps(part.model_dump(mode="json"), default=str))
        return size
    return len(str(content))


def message_size(message: LLMMessage) -> int:
    """A message's share of the budget, in characters."""
    if isinstance(message, AssistantMessage):
        size = _content_size(message.content)
        for tool_call in message.tool_calls or ():
            arguments = tool_call.function.arguments
            size += len(tool_call.function.name) + len(
                arguments if isinstance(arguments, str) else json.dumps(arguments)
            )
        return size
    if isinstance(message, ToolMessage | SystemMessage | ErrorMessage):
        return len(message.content or "")
    if isinstance(message, UserMessage):
        return _content_size(message.content)
    return len(str(message))


def messages_size(messages: Sequence[LLMMessage]) -> int:
    return sum(message_size(message) for message in messages)


def select_turns(
    turns: Sequence[HistoryTurn],
    sizes: dict[str, int],
    *,
    limits: HistoryLimits,
    mandatory: Collection[str],
) -> list[HistoryTurn]:
    """The turns that fit, oldest first.

    The newest ``min_turns`` turns and every mandatory turn always stay. Older
    turns are then taken newest first while the window stays within the budget;
    the first that does not fit ends the window, so it stays contiguous back
    from the newest turn. The active turn is not part of the window and does
    not count against it, so the window does not depend on how far a turn has
    run.
    """
    total = sum(sizes[turn.key] for turn in turns if turn.key in mandatory)
    kept: set[str] = set(mandatory)
    taken = 0
    for turn in reversed(turns):
        if turn.key in mandatory:
            continue
        size = sizes[turn.key]
        if taken >= limits.min_turns and total + size > limits.budget_chars:
            break
        kept.add(turn.key)
        total += size
        taken += 1
    return [turn for turn in turns if turn.key in kept]


class HistoryWindowLoader:
    """Builds a request's history window from stored rows."""

    def __init__(
        self,
        render: Callable[[list[LLMMessage]], Awaitable[list[LLMMessage]]],
    ) -> None:
        self._render = render

    async def _render_turn(self, turn: HistoryTurn) -> list[LLMMessage]:
        return await self._render([row.message for row in turn.rows])

    async def load(
        self,
        db: Database,
        *,
        interface_type: str,
        conversation_id: str,
        processing_profile_id: str,
        subconversation_id: str | None,
        limits: HistoryLimits,
        now: datetime,
        active_turn_id: str | None,
        thread_root_id: int | None = None,
        referenced_row_ids: Collection[int] = (),
    ) -> HistoryWindow:
        """Load the window for a request on this conversation.

        ``active_turn_id`` names the turn being run; its rows, whatever is
        already stored of it, are rendered whole and apart from the window.
        ``thread_root_id`` is the thread a reply points at: its turns on this
        profile join the window whatever their age or size, as do the turns
        holding ``referenced_row_ids`` (pinned rows, the message replied to).
        """
        history = db.message_history
        recent_rows = await history.get_history_window_rows(
            interface_type=interface_type,
            conversation_id=conversation_id,
            processing_profile_id=processing_profile_id,
            subconversation_id=subconversation_id,
            since=now - limits.max_age,
            row_limit=WINDOW_ROW_LIMIT,
            exclude_turn_id=active_turn_id,
        )
        explicit_rows: list[MessageWithMetadata] = []
        if thread_root_id is not None:
            explicit_rows.extend(
                await history.get_thread_turn_rows(
                    thread_root_id=thread_root_id,
                    processing_profile_id=processing_profile_id,
                    subconversation_id=subconversation_id,
                )
            )
        if referenced_row_ids:
            explicit_rows.extend(
                await history.get_turn_rows_with_metadata(
                    internal_ids=referenced_row_ids
                )
            )
        active_rows = (
            await history.get_turn_rows_with_metadata(turn_ids=[active_turn_id])
            if active_turn_id is not None
            else []
        )

        cutoff = now - limits.max_age
        explicit_keys = {
            turn_key(row) for row in explicit_rows if row.turn_id != active_turn_id
        }
        turns = [
            turn
            for turn in group_turns([
                *recent_rows,
                *(row for row in explicit_rows if row.turn_id != active_turn_id),
            ])
            if turn.key in explicit_keys or turn.last_activity >= cutoff
        ]
        rendered = {turn.key: await self._render_turn(turn) for turn in turns}
        active_messages = await self._render([row.message for row in active_rows])
        kept = select_turns(
            turns,
            {key: messages_size(messages) for key, messages in rendered.items()},
            limits=limits,
            mandatory=explicit_keys,
        )
        return HistoryWindow(
            turns=tuple(kept),
            messages=[message for turn in kept for message in rendered[turn.key]],
            active_messages=active_messages,
        )
