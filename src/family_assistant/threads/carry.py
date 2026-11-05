"""What "Continue in a thread" carries: the Home turns on offer, and which to take.

The candidates are the anchor's turn and the Home turns just before it, as many
as ``CONTINUE_CONTEXT_TURNS`` allows. Home is a feed of unrelated messages, so
the turns before the anchor are often about something else; Jev's turn
relevance, the classifier compaction uses to keep the turns a new message
continues, suggests which of them belong with the anchor. The user sees the
suggestion as ticks in the dialog and has the last word, so a wrong answer costs
an untick, and a timeout or failure falls back to offering every candidate
ticked. See docs/design/continue-carry-preview.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from family_assistant.threads.creation import CONTINUE_CONTEXT_TURNS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from family_assistant.processing.history_relevance import TurnRelevance
    from family_assistant.storage.database import DatabaseExecutor
    from family_assistant.storage.types import MessageHistoryRow

logger = logging.getLogger(__name__)

SuggestionSource = Literal["assistant", "default"]

_PREVIEW_CHARS = 600


@dataclass(frozen=True, slots=True)
class CarryCandidate:
    """One Home turn "Continue in a thread" could copy, as the dialog shows it."""

    turn_id: str
    started_at: datetime
    user_text: str | None
    """What the user wrote, when the turn is one they started."""
    reply_text: str | None
    """The turn's final reply, or the echoed outcome of a thread."""
    is_anchor: bool
    is_echo: bool


@dataclass(frozen=True, slots=True)
class CarrySuggestion:
    """The candidates, oldest first, and which of them to tick."""

    candidates: list[CarryCandidate]
    suggested_turn_ids: frozenset[str]
    source: SuggestionSource


async def carry_window(
    db: DatabaseExecutor, *, interface_type: str, home: str, anchor_turn_id: str
) -> list[str]:
    """The turn ids "Continue in a thread" may copy, oldest first, the anchor's last."""
    return await db.message_history.turn_ids_leading_to(
        interface_type=interface_type,
        conversation_id=home,
        turn_id=anchor_turn_id,
        limit=CONTINUE_CONTEXT_TURNS,
    )


async def carry_candidates(
    db: DatabaseExecutor, *, interface_type: str, home: str, anchor_turn_id: str
) -> list[CarryCandidate]:
    """The Home turns on offer for a continuation from ``anchor_turn_id``."""
    turn_ids = await carry_window(
        db, interface_type=interface_type, home=home, anchor_turn_id=anchor_turn_id
    )
    rows = await db.message_history.top_level_turn_rows(
        interface_type=interface_type, conversation_id=home, turn_ids=turn_ids
    )
    by_turn: dict[str, list[MessageHistoryRow]] = {}
    for row in rows:
        assert row["turn_id"] is not None
        by_turn.setdefault(row["turn_id"], []).append(row)
    return [
        _candidate(turn_id, by_turn[turn_id], is_anchor=turn_id == anchor_turn_id)
        for turn_id in turn_ids
        if turn_id in by_turn
    ]


def _candidate(
    turn_id: str, rows: Sequence[MessageHistoryRow], *, is_anchor: bool
) -> CarryCandidate:
    user_text = next(
        (
            row["content"]
            for row in rows
            if row["role"] == "user" and not row["is_internal"] and row["content"]
        ),
        None,
    )
    replies = [
        row
        for row in rows
        if row["role"] == "assistant" and not row["tool_calls"] and row["content"]
    ]
    reply = replies[-1] if replies else None
    return CarryCandidate(
        turn_id=turn_id,
        started_at=min(row["timestamp"] for row in rows),
        user_text=_shortened(user_text),
        reply_text=_shortened(reply["content"]) if reply else None,
        is_anchor=is_anchor,
        is_echo=reply is not None and reply["echoed_from_conversation_id"] is not None,
    )


def _shortened(text: str | None) -> str | None:
    if text is None or len(text) <= _PREVIEW_CHARS:
        return text
    return f"{text[:_PREVIEW_CHARS].rstrip()}…"


async def suggest_carried_turns(
    candidates: list[CarryCandidate], relevance: TurnRelevance | None
) -> CarrySuggestion:
    """Which candidates to tick: the anchor, and the turns Jev says go with it.

    Without Jev, in shadow mode, or when it fails, every candidate is ticked,
    which is what a continuation carried before there was a choice.
    """
    everything = CarrySuggestion(
        candidates=candidates,
        suggested_turn_ids=frozenset(c.turn_id for c in candidates),
        source="default",
    )
    anchor = next((c for c in candidates if c.is_anchor), None)
    earlier = [c for c in candidates if not c.is_anchor]
    if relevance is None or anchor is None or not earlier:
        return everything
    outcome = await relevance.assess(
        request=_turn_text(anchor),
        turns=[(c.turn_id, c.user_text or "", c.reply_text or "") for c in earlier],
    )
    logger.info("Continue-in-thread relevance: %s", outcome.to_json())
    if outcome.outcome != "decided" or not outcome.active:
        return everything
    return CarrySuggestion(
        candidates=candidates,
        suggested_turn_ids=outcome.relevant_keys | {anchor.turn_id},
        source="assistant",
    )


def _turn_text(candidate: CarryCandidate) -> str:
    return "\n\n".join(
        text for text in (candidate.user_text, candidate.reply_text) if text
    )
