"""Rendering the unreviewed stretch of a conversation for the curator.

See docs/design/conversation-memory.md, "A review covers a bounded chunk of
completed turns" and "The transcript is input, and its provenance travels with
it". The curator never fetches history: the review task renders the rows into
the request text, the way a delegation carries its request. This module is that
rendering, and it is pure -- it takes rows and returns text, so the cutting
rules can be tested against a table of turns rather than against a database.

**The rules, and why each exists.**

- *Cut on a turn boundary.* A turn is a request and its outcome; showing the
  request without the outcome invites the curator to record a plan as a
  decision. A chunk therefore ends between turns, never inside one.
- *An unfinished turn ends the chunk before itself* -- one parked on a
  confirmation, or cut off by a restart -- because its outcome may still be
  coming. Once a **later** turn has finished, the earlier one never will in any
  way worth waiting for, so it is rendered with a marker saying so and passed.
  That is the whole turn-lifecycle model in v1.
- *A single turn larger than the budget is rendered truncated*, with a marker,
  rather than blocking its conversation for ever.
- *Tool result bodies are omitted.* Tool output is where injected text lives,
  and the user's words and the assistant's replies carry what mattered. A tool
  *call* may be named, so the curator can see that the assistant looked
  something up, without being shown what came back.

Rows with no ``turn_id`` are each their own complete unit. The column is
nullable for rows written outside a turn, which have no request-and-outcome
pairing to wait for; grouping them with a neighbour would make an unrelated row
decide whether that neighbour's turn had finished.

Every line carries its ``#internal_id`` so the curator can cite it, and the
apply path can check the citation against the stretch it was shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from family_assistant.storage.types import MessageHistoryRow

UNFINISHED_TURN_MARKER = "[this turn never finished]"
TRUNCATED_TURN_MARKER = "[this turn was too long to show in full and is cut off here]"

_ROLE_LABELS = {"assistant": "Assistant", "system": "System"}


@dataclass(frozen=True)
class RenderedStretch:
    """One chunk of transcript, and what it covers.

    Attributes:
        text: The rendered chunk, empty when no complete chunk was available.
        rows: Every row the chunk covers, tool results included. What the
            curator was *shown* is narrower than what the chunk *covers*; the
            evidence scope and the merged taint are both about the coverage.
        first_internal_id: The chunk's first covered row, or 0 when empty.
        last_internal_id: The chunk's last covered row, which is what the
            watermark advances to. 0 when empty.
        user_char_count: Characters of text a person actually wrote, which is
            what the skip measurement counts.
        user_row_count: How many rows a person actually wrote.
        rows_remain: Whether eligible rows were left after this chunk, so the
            conversation is still due.
    """

    text: str
    rows: tuple[MessageHistoryRow, ...]
    first_internal_id: int
    last_internal_id: int
    user_char_count: int
    user_row_count: int
    rows_remain: bool

    @property
    def is_empty(self) -> bool:
        """Whether no complete chunk was available yet."""
        return not self.rows


@dataclass(frozen=True)
class _Turn:
    """One turn's rows, and whether it reached a terminal reply."""

    rows: tuple[MessageHistoryRow, ...]
    complete: bool


def render_stretch(
    rows: Sequence[MessageHistoryRow],
    *,
    budget_chars: int,
    completed_turn_ids: Collection[str],
    sender_label: Callable[[MessageHistoryRow], str],
) -> RenderedStretch:
    """Render as much of ``rows`` as one review may read.

    Args:
        rows: The eligible unreviewed rows, oldest first, as
            ``select_review_rows`` returns them.
        budget_chars: The rendered size one review's transcript may reach.
        completed_turn_ids: The turn ids that have a terminal assistant reply.
            Supplied rather than derived, because completeness is a fact about
            the whole turn and rows of it may lie before the watermark.
        sender_label: How to name the person who wrote a user row. A hook
            because the answer differs by interface: milestone 4 fills in
            Telegram sender names, and until then a row offers a user name or
            an id.

    Returns:
        The chunk, empty when the first available turn has not finished and no
        later turn has either -- the conversation is still due, and nothing is
        reviewed or passed.
    """
    turns = _group_into_turns(rows, completed_turn_ids)
    if not turns:
        return _empty()

    rendered: list[str] = []
    covered: list[MessageHistoryRow] = []
    used = 0

    for index, turn in enumerate(turns):
        if not turn.complete and not any(
            later.complete for later in turns[index + 1 :]
        ):
            break

        block = _render_turn(turn, sender_label=sender_label)
        if covered and used + len(block) + 1 > budget_chars:
            break
        if not covered and len(block) > budget_chars:
            block = _truncate(block, budget_chars)

        rendered.append(block)
        covered.extend(turn.rows)
        used += len(block) + 1

    if not covered:
        return _empty()

    return RenderedStretch(
        text="\n".join(rendered),
        rows=tuple(covered),
        first_internal_id=int(covered[0]["internal_id"]),
        last_internal_id=int(covered[-1]["internal_id"]),
        user_char_count=sum(
            len(row["content"] or "") for row in covered if _is_user(row)
        ),
        user_row_count=sum(1 for row in covered if _is_user(row)),
        rows_remain=len(covered) < len(rows),
    )


def default_sender_label(row: MessageHistoryRow) -> str:
    """Who wrote a user row, from what the row itself offers.

    Milestone 1 has no per-interface name resolution: a web row carries the
    signed-in user's stored id and nothing better, and falling back to "User"
    is honest where even that is missing.
    """
    return str(row.get("user_id") or "User")


def _empty() -> RenderedStretch:
    return RenderedStretch(
        text="",
        rows=(),
        first_internal_id=0,
        last_internal_id=0,
        user_char_count=0,
        user_row_count=0,
        rows_remain=True,
    )


def _group_into_turns(
    rows: Sequence[MessageHistoryRow], completed_turn_ids: Collection[str]
) -> list[_Turn]:
    """Split rows into consecutive same-turn groups, oldest first."""
    groups: list[_Turn] = []
    current: list[MessageHistoryRow] = []
    current_turn: str | None = None

    def _close() -> None:
        if current:
            groups.append(
                _Turn(
                    rows=tuple(current),
                    complete=current_turn is None or current_turn in completed_turn_ids,
                )
            )
            current.clear()

    for row in rows:
        turn_id = row.get("turn_id")
        key = str(turn_id) if turn_id is not None else None
        if current and (key is None or key != current_turn):
            _close()
        current_turn = key
        current.append(row)
    _close()
    return groups


def _render_turn(
    turn: _Turn, *, sender_label: Callable[[MessageHistoryRow], str]
) -> str:
    """One turn's visible lines, with the never-finished marker where it applies."""
    lines = [
        line
        for row in turn.rows
        if (line := _render_row(row, sender_label=sender_label)) is not None
    ]
    if not turn.complete:
        lines.append(f"  {UNFINISHED_TURN_MARKER}")
    return "\n".join(lines)


def _render_row(
    row: MessageHistoryRow, *, sender_label: Callable[[MessageHistoryRow], str]
) -> str | None:
    """One transcript line, or None for a row the curator is not shown."""
    role = str(row["role"])
    if role == "tool":
        return None

    prefix = f"#{row['internal_id']} {_timestamp(row)}"
    if role == "user":
        speaker = "System" if row.get("is_internal") else sender_label(row)
        return f"{prefix} {speaker}: {row['content'] or ''}".rstrip()

    label = _ROLE_LABELS.get(role, role.capitalize())
    parts: list[str] = []
    if row["content"]:
        parts.append(str(row["content"]))
    called = _tool_names(row)
    if called:
        parts.append(f"(called {', '.join(called)})")
    if not parts:
        return None
    return f"{prefix} {label}: {' '.join(parts)}"


def _tool_names(row: MessageHistoryRow) -> list[str]:
    """The tools an assistant row called, named but not answered."""
    return [call.function.name for call in row.get("tool_calls") or []]


def _timestamp(row: MessageHistoryRow) -> str:
    """The row's moment in UTC, which is what production writers persist."""
    moment = row["timestamp"]
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%d %H:%M")


def _is_user(row: MessageHistoryRow) -> bool:
    """Whether a person wrote this row, as the due predicate counts it."""
    return str(row["role"]) == "user" and not row.get("is_internal")


def _truncate(block: str, budget_chars: int) -> str:
    """Cut an over-budget turn to fit, saying so where it was cut."""
    marker = f"\n  {TRUNCATED_TURN_MARKER}"
    keep = max(0, budget_chars - len(marker))
    return block[:keep].rstrip() + marker
