"""The prompt's history window, built from whole turns and changed only at events.

A turn is the rows its own turn wrote; a row with no turn id (a proactive send,
a pinned data row) is a turn of its own. A turn is never split, so a window can
never start with a tool result whose call it lost, or keep an answer without the
request it answered.

Between compaction events the window only appends: the turns the latest event
kept render as it decided, and every turn after it renders verbatim. An event
runs at the start of a turn whenever the window has to change -- it outgrew its
budget, a turn in it passed the age cap, the request points at a turn outside
it, or (where configured) the conversation was idle long enough for provider
caches to have expired -- and decides every turn once more. See
docs/design/history-compaction.md.

Every path that needs the window -- the turn itself, its context-length retry,
and the web producer's taint computation -- goes through
:class:`HistoryWindowLoader`, so they agree on which rows are in the prompt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
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
from family_assistant.processing.history_compaction import (
    COMPACTION_TARGET_RATIO,
    VERBATIM,
    CompactionCandidate,
    CompactionCapabilities,
    CompactionPlan,
    CompactionReason,
    can_compact,
    plan_compaction,
    render_compacted_turn,
    without_bound_thinking,
)
from family_assistant.storage.history_compaction import TurnDecision, TurnMode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from family_assistant.llm.messages import MessageWithMetadata
    from family_assistant.processing.history_relevance import TurnRelevance
    from family_assistant.storage.database import Database
    from family_assistant.storage.history_compaction import HistoryScope
    from family_assistant.storage.repositories.history_compaction import (
        CompactionEvent,
    )

logger = logging.getLogger(__name__)

# What an inlined image counts for against the budget. Images are re-sent as
# bytes, which no character count measures; this is roughly what a provider
# charges for a typical photo, in characters of text.
IMAGE_CHAR_COST = 4_000

# The most rows one window read fetches before whole turns are completed. A
# window that reaches it is over any budget and compacts; it bounds the first
# read of a long conversation, before any event has been recorded for it.
WINDOW_ROW_LIMIT = 2_000


@dataclass(frozen=True, slots=True)
class HistoryLimits:
    """How much history a window may hold, and when it is re-decided."""

    budget_chars: int
    min_turns: int
    max_age: timedelta
    # The first message after this much quiet is a compaction event: provider
    # caches have expired by then, so changing the prefix costs nothing extra.
    idle_gap: timedelta | None = None


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One turn's rows, oldest first."""

    key: str
    rows: tuple[MessageWithMetadata, ...]

    @property
    def last_activity(self) -> datetime:
        return max(row.timestamp for row in self.rows)

    @property
    def first_internal_id(self) -> int:
        return min(int(row.internal_id) for row in self.rows)

    @property
    def is_complete(self) -> bool:
        """Whether the turn has finished: it has a final reply or an error.

        A row written outside any turn is complete by itself. A turn whose last
        assistant row still calls tools was interrupted, or is still running
        concurrently, and more of it may yet be written.
        """
        if self.key.startswith("row:"):
            return True
        return any(
            isinstance(row.message, ErrorMessage)
            or (
                isinstance(row.message, AssistantMessage) and not row.message.tool_calls
            )
            for row in self.rows
        )


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """The rendered history of one request.

    ``turns`` are the completed turns in the window, oldest first, and
    ``messages`` their rendering. ``active_messages`` renders the turn being
    run, which is always sent whole and is never part of ``turns``.
    ``compaction`` is the reason when this load ran a compaction event.
    """

    turns: tuple[HistoryTurn, ...]
    messages: list[LLMMessage]
    active_messages: list[LLMMessage]
    compaction: CompactionReason | None = None


@dataclass(slots=True)
class _WindowTurn:
    turn: HistoryTurn
    decision: TurnDecision | None
    explicit: bool
    open: bool = False
    """Unfinished: rendered as it is, and left out of any event's decision."""
    rendered: list[LLMMessage] = field(default_factory=list)


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


def _text_of(rows: Sequence[MessageWithMetadata], role: type[LLMMessage]) -> str:
    """The newest text a message of *role* carries in *rows*, or empty.

    For a turn, the user's request and the assistant's final answer: what the
    relevance classifier is shown of it.
    """
    for row in reversed(rows):
        message = row.message
        if not isinstance(message, role):
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            text = " ".join(
                part.text for part in content if isinstance(part, TextContentPart)
            )
            if text:
                return text
    return ""


class HistoryWindowLoader:
    """Builds a request's history window from stored rows and recorded events."""

    def __init__(
        self,
        *,
        render: Callable[[list[LLMMessage]], Awaitable[list[LLMMessage]]],
        timezone: ZoneInfo,
        capabilities: Callable[[], Awaitable[CompactionCapabilities]],
        relevance: TurnRelevance | None = None,
    ) -> None:
        self._render = render
        self._timezone = timezone
        self._capabilities = capabilities
        self._relevance = relevance

    async def _render_verbatim(
        self, turn: HistoryTurn, strip_through: int | None
    ) -> list[LLMMessage]:
        return await self._render([
            without_bound_thinking(row.message)
            if strip_through is not None and int(row.internal_id) <= strip_through
            else row.message
            for row in turn.rows
        ])

    async def _render_turn(
        self, turn: HistoryTurn, decision: TurnDecision
    ) -> list[LLMMessage]:
        if decision.mode is TurnMode.COMPACTED:
            return render_compacted_turn(turn.rows, self._timezone)
        return await self._render_verbatim(turn, decision.strip_through)

    async def load(
        self,
        db: Database,
        *,
        scope: HistoryScope,
        limits: HistoryLimits,
        now: datetime,
        active_turn_id: str | None,
        thread_root_id: int | None = None,
        referenced_row_ids: Collection[int] = (),
        context_length_target: int | None = None,
        record: bool = True,
    ) -> HistoryWindow:
        """Load the window for a request on this conversation.

        ``active_turn_id`` names the turn being run; its rows, whatever is
        already stored of it, are rendered whole and apart from the window.
        ``thread_root_id`` is the thread a reply points at: its turns on this
        profile join the window whatever their age or size, as do the turns
        holding ``referenced_row_ids`` (pinned rows, the message replied to).

        ``context_length_target`` forces a compaction event down to that many
        characters: the retry after a provider rejected the prompt as too long.

        ``record=False`` computes the window a turn starting now would get,
        compaction included, without recording the event: for a reader that
        needs to know what will be in the prompt, not to fix it.
        """
        history = db.message_history
        event = await db.history_compaction.latest(scope)
        boundary = event.boundary_internal_id if event is not None else 0
        decided = event.decisions if event is not None else {}

        uncovered_rows = await history.get_history_window_rows(
            interface_type=scope.interface_type,
            conversation_id=scope.conversation_id,
            processing_profile_id=scope.processing_profile_id,
            subconversation_id=scope.subconversation_id,
            after_internal_id=boundary,
            row_limit=WINDOW_ROW_LIMIT,
            exclude_turn_id=active_turn_id,
        )
        truncated = len(uncovered_rows) >= WINDOW_ROW_LIMIT
        kept_turn_ids = [key for key in decided if not key.startswith("row:")]
        kept_row_ids = [int(key[4:]) for key in decided if key.startswith("row:")]
        if event is not None:
            kept_turn_ids.extend(event.open_turn_keys)
        decided_rows = await history.get_turn_rows_with_metadata(
            interface_type=scope.interface_type,
            conversation_id=scope.conversation_id,
            subconversation_id=scope.subconversation_id,
            turn_ids=kept_turn_ids,
            internal_ids=kept_row_ids,
        )
        explicit_rows: list[MessageWithMetadata] = []
        if thread_root_id is not None:
            explicit_rows.extend(
                await history.get_thread_turn_rows(
                    interface_type=scope.interface_type,
                    conversation_id=scope.conversation_id,
                    thread_root_id=thread_root_id,
                    processing_profile_id=scope.processing_profile_id,
                    subconversation_id=scope.subconversation_id,
                )
            )
        if referenced_row_ids:
            explicit_rows.extend(
                await history.get_turn_rows_with_metadata(
                    interface_type=scope.interface_type,
                    conversation_id=scope.conversation_id,
                    subconversation_id=scope.subconversation_id,
                    internal_ids=referenced_row_ids,
                )
            )
        active_rows = (
            await history.get_turn_rows_with_metadata(
                interface_type=scope.interface_type,
                conversation_id=scope.conversation_id,
                subconversation_id=scope.subconversation_id,
                turn_ids=[active_turn_id],
            )
            if active_turn_id is not None
            else []
        )

        explicit_keys = {
            turn_key(row) for row in explicit_rows if row.turn_id != active_turn_id
        }
        window: list[_WindowTurn] = []
        for turn in group_turns([
            *uncovered_rows,
            *decided_rows,
            *(row for row in explicit_rows if row.turn_id != active_turn_id),
        ]):
            if turn.key == active_turn_id:
                continue
            decision = self._prior_decision(turn, event, decided, boundary)
            explicit = turn.key in explicit_keys
            if decision is None and not explicit:
                continue
            window.append(
                _WindowTurn(
                    turn=turn,
                    decision=decision,
                    explicit=explicit,
                    # An event decides only completed turns; one that has not
                    # finished could be compacted or dropped before its last
                    # rows are written. Past the age cap it is abandoned.
                    open=not turn.is_complete
                    and turn.last_activity >= now - limits.max_age,
                )
            )
        for entry in window:
            if entry.decision is not None:
                entry.rendered = await self._render_turn(entry.turn, entry.decision)

        reason = context_length_target and CompactionReason.CONTEXT_LENGTH
        reason = reason or self._trigger(
            window, limits=limits, now=now, truncated=truncated
        )
        compaction: CompactionReason | None = None
        active_strip_through = (
            boundary
            if event is not None
            and event.changed
            and active_turn_id is not None
            and active_turn_id in event.open_turn_keys
            else None
        )
        if reason is not None:
            new_boundary = max(
                (
                    int(row.internal_id)
                    for row in (
                        *uncovered_rows,
                        *decided_rows,
                        *explicit_rows,
                        *active_rows,
                    )
                ),
                default=boundary,
            )
            plan = await self._compact(
                db,
                window,
                scope=scope,
                limits=limits,
                now=now,
                reason=reason,
                boundary=new_boundary,
                active_turn_id=active_turn_id,
                active_rows=active_rows,
                record=record,
                target_chars=(
                    context_length_target
                    if context_length_target is not None
                    else int(limits.budget_chars * COMPACTION_TARGET_RATIO)
                ),
            )
            for entry in window:
                if entry.open:
                    entry.decision = TurnDecision(
                        TurnMode.VERBATIM,
                        strip_through=new_boundary
                        if plan.changed
                        else entry.decision and entry.decision.strip_through,
                    )
                else:
                    entry.decision = plan.decisions.get(entry.turn.key)
                if entry.decision is not None:
                    entry.rendered = await self._render_turn(entry.turn, entry.decision)
            if plan.changed:
                active_strip_through = new_boundary
            compaction = reason

        kept = [entry for entry in window if entry.decision is not None]
        active_turn = group_turns(active_rows)
        active_messages = (
            await self._render_verbatim(active_turn[0], active_strip_through)
            if active_turn
            else []
        )
        return HistoryWindow(
            turns=tuple(entry.turn for entry in kept),
            messages=[message for entry in kept for message in entry.rendered],
            active_messages=active_messages,
            compaction=compaction,
        )

    @staticmethod
    def _prior_decision(
        turn: HistoryTurn,
        event: CompactionEvent | None,
        decided: dict[str, TurnDecision],
        boundary: int,
    ) -> TurnDecision | None:
        """How the turn rendered before this load; ``None`` if it was dropped."""
        if event is None:
            return VERBATIM
        if turn.key in event.open_turn_keys:
            return TurnDecision(
                TurnMode.VERBATIM, strip_through=boundary if event.changed else None
            )
        if turn.first_internal_id > boundary:
            return VERBATIM
        return decided.get(turn.key)

    @staticmethod
    def _trigger(
        window: Sequence[_WindowTurn],
        *,
        limits: HistoryLimits,
        now: datetime,
        truncated: bool,
    ) -> CompactionReason | None:
        """Why the window has to change at this request, or ``None``."""
        in_window = [entry for entry in window if entry.decision is not None]
        if any(entry.decision is None for entry in window):
            return CompactionReason.REFERENCE
        cutoff = now - limits.max_age
        if any(
            entry.turn.last_activity < cutoff and not entry.explicit and not entry.open
            for entry in in_window
        ):
            return CompactionReason.AGE
        size = sum(messages_size(entry.rendered) for entry in in_window)
        if truncated or size > limits.budget_chars:
            return CompactionReason.BUDGET
        if (
            limits.idle_gap is not None
            and in_window
            and now - max(entry.turn.last_activity for entry in in_window)
            > limits.idle_gap
        ):
            return CompactionReason.IDLE
        return None

    async def _compact(
        self,
        db: Database,
        window: Sequence[_WindowTurn],
        *,
        scope: HistoryScope,
        limits: HistoryLimits,
        now: datetime,
        reason: CompactionReason,
        boundary: int,
        active_turn_id: str | None,
        active_rows: Sequence[MessageWithMetadata],
        target_chars: int,
        record: bool,
    ) -> CompactionPlan:
        capabilities = await self._capabilities()
        candidates: list[CompactionCandidate] = []
        for entry in window:
            if entry.open:
                continue
            verbatim = (
                entry.rendered
                if entry.decision is not None
                and entry.decision.mode is TurnMode.VERBATIM
                else await self._render_verbatim(entry.turn, None)
            )
            compacted_size = (
                messages_size(render_compacted_turn(entry.turn.rows, self._timezone))
                if can_compact(entry.turn.rows, capabilities)
                else None
            )
            candidates.append(
                CompactionCandidate(
                    key=entry.turn.key,
                    last_activity=entry.turn.last_activity,
                    verbatim_size=messages_size(verbatim),
                    compacted_size=compacted_size,
                    previous=entry.decision,
                    explicit=entry.explicit,
                )
            )
        cutoff = now - limits.max_age
        details: dict[str, object] = {
            "target_chars": target_chars,
            "budget_chars": limits.budget_chars,
            "size_before": sum(
                messages_size(entry.rendered)
                for entry in window
                if entry.decision is not None
            ),
        }
        relevant: frozenset[str] = frozenset()
        # A read that does not record the event needs the answers only where
        # they change the plan; in shadow mode they would only be recorded.
        if (
            self._relevance is not None
            and window
            and (record or self._relevance.active)
        ):
            outcome = await self._relevance.assess(
                request=_text_of(active_rows, UserMessage),
                turns=[
                    (
                        entry.turn.key,
                        _text_of(entry.turn.rows, UserMessage),
                        _text_of(entry.turn.rows, AssistantMessage),
                    )
                    for entry in window
                    if not entry.open
                ],
            )
            details["relevance"] = outcome.to_json()
            if outcome.active:
                relevant = outcome.relevant_keys
            else:
                shadow = plan_compaction(
                    candidates,
                    target_chars=target_chars,
                    min_turns=limits.min_turns,
                    cutoff=cutoff,
                    boundary_internal_id=boundary,
                    relevant=outcome.relevant_keys,
                )
                details["relevance_would_decide"] = {
                    key: decision.mode.value
                    for key, decision in shadow.decisions.items()
                }
        plan = plan_compaction(
            candidates,
            target_chars=target_chars,
            min_turns=limits.min_turns,
            cutoff=cutoff,
            boundary_internal_id=boundary,
            relevant=relevant,
        )
        details["size_after"] = sum(
            c.compacted_size
            if plan.decisions[c.key].mode is TurnMode.COMPACTED
            and c.compacted_size is not None
            else c.verbatim_size
            for c in candidates
            if c.key in plan.decisions
        )
        if not record:
            return plan
        await db.history_compaction.record(
            scope,
            now=now,
            boundary_internal_id=boundary,
            open_turn_keys={
                *(entry.turn.key for entry in window if entry.open),
                *([active_turn_id] if active_turn_id is not None else []),
            },
            reason=reason.value,
            decisions=plan.decisions,
            changed=plan.changed,
            details=details,
        )
        logger.info(
            "History compaction (%s) for %s/%s on %s: %d of %d turns kept, "
            "%s -> %s chars, changed=%s",
            reason.value,
            scope.interface_type,
            scope.conversation_id,
            scope.processing_profile_id,
            len(plan.decisions),
            len(candidates),
            details["size_before"],
            details["size_after"],
            plan.changed,
        )
        return plan
