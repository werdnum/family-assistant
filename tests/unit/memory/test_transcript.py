"""The rules by which a review's chunk is cut and rendered.

Slice 5 of docs/design/conversation-memory.md, "A review covers a bounded chunk
of completed turns". The renderer is pure, so every rule is exercised against a
table of rows rather than against a conversation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.memory.transcript import (
    TRUNCATED_TURN_MARKER,
    UNFINISHED_TURN_MARKER,
    RenderedStretch,
    default_sender_label,
    render_stretch,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from family_assistant.storage.types import MessageHistoryRow

NOW = datetime(2026, 9, 17, 14, 3, tzinfo=UTC)

_COUNTER = [0]


def _row(
    role: str,
    content: str | None = None,
    *,
    turn_id: str | None = "turn-1",
    user_id: str | None = "alice",
    is_internal: bool = False,
    tool_calls: list[ToolCallItem] | None = None,
    internal_id: int | None = None,
) -> MessageHistoryRow:
    """One deserialized message-history row, with only what the renderer reads."""
    if internal_id is None:
        _COUNTER[0] += 1
        internal_id = _COUNTER[0]
    # ast-grep-ignore: no-dict-any - a partial row literal, cast to the TypedDict below
    row: dict[str, Any] = {
        "internal_id": internal_id,
        "role": role,
        "content": content,
        "turn_id": turn_id,
        "timestamp": NOW,
        "user_id": user_id,
        "is_internal": is_internal,
        "tool_calls": tool_calls,
    }
    return cast("MessageHistoryRow", row)


def _tool_call(name: str) -> ToolCallItem:
    return ToolCallItem(
        id="call-1",
        type="function",
        function=ToolCallFunction(name=name, arguments="{}"),
    )


def _render(
    rows: Sequence[MessageHistoryRow],
    *,
    budget_chars: int = 10_000,
    completed: Collection[str] = ("turn-1", "turn-2", "turn-3"),
) -> RenderedStretch:
    return render_stretch(
        rows,
        budget_chars=budget_chars,
        completed_turn_ids=completed,
        sender_label=default_sender_label,
    )


def test_a_user_line_names_its_sender_its_id_and_its_time() -> None:
    rendered = _render([_row("user", "we always take the tram", internal_id=41)])

    assert rendered.text == "#41 2026-09-17 14:03 alice: we always take the tram"


def test_a_tool_result_body_is_never_shown_but_its_call_is_named() -> None:
    rendered = _render([
        _row("assistant", "checking", tool_calls=[_tool_call("search_web")]),
        _row("tool", "seventeen hotels with a separate sleeping area"),
        _row("assistant", "here are three"),
    ])

    assert "seventeen hotels" not in rendered.text
    assert "(called search_web)" in rendered.text


def test_a_tool_result_row_is_still_covered_by_the_chunk() -> None:
    """What the curator is shown is narrower than what the chunk covers.

    The evidence scope and the merged taint are both about the coverage, so a
    tool row that is not rendered must still move the watermark past itself and
    contribute its provenance.
    """
    rows = [
        _row("user", "find us a hotel", internal_id=10),
        _row("assistant", "", tool_calls=[_tool_call("search_web")], internal_id=11),
        _row("tool", "injected text", internal_id=12),
        _row("assistant", "here are three", internal_id=13),
    ]

    rendered = _render(rows)

    assert rendered.last_internal_id == 13
    assert len(rendered.rows) == 4


def test_an_unfinished_turn_ends_the_chunk_before_itself() -> None:
    rows = [
        _row("user", "settled question", turn_id="turn-1"),
        _row("assistant", "settled answer", turn_id="turn-1"),
        _row("user", "parked question", turn_id="turn-2"),
    ]

    rendered = _render(rows, completed=("turn-1",))

    assert "parked question" not in rendered.text
    assert rendered.rows_remain is True


def test_nothing_is_rendered_when_the_first_turn_has_not_finished() -> None:
    rendered = _render([_row("user", "parked", turn_id="turn-2")], completed=())

    assert rendered.is_empty
    assert rendered.last_internal_id == 0


def test_an_unfinished_turn_is_passed_with_a_marker_once_a_later_one_completes() -> (
    None
):
    rows = [
        _row("user", "parked question", turn_id="turn-1"),
        _row("user", "later question", turn_id="turn-2"),
        _row("assistant", "later answer", turn_id="turn-2"),
    ]

    rendered = _render(rows, completed=("turn-2",))

    assert UNFINISHED_TURN_MARKER in rendered.text
    assert "parked question" in rendered.text
    assert "later answer" in rendered.text
    assert rendered.rows_remain is False


def test_a_chunk_ends_on_a_turn_boundary_under_the_budget() -> None:
    rows = [
        _row("user", "a" * 200, turn_id="turn-1"),
        _row("assistant", "b" * 200, turn_id="turn-1"),
        _row("user", "c" * 200, turn_id="turn-2"),
        _row("assistant", "d" * 200, turn_id="turn-2"),
    ]

    rendered = _render(rows, budget_chars=600)

    assert "c" * 200 not in rendered.text
    assert "b" * 200 in rendered.text
    assert rendered.rows_remain is True


def test_a_single_turn_over_the_budget_is_truncated_rather_than_blocking() -> None:
    rows = [_row("user", "a" * 5000, turn_id="turn-1")]

    rendered = _render(rows, budget_chars=200)

    assert TRUNCATED_TURN_MARKER in rendered.text
    assert len(rendered.text) <= 200
    assert rendered.rows_remain is False


def test_a_row_without_a_turn_id_is_its_own_complete_unit() -> None:
    """A row written outside a turn has no outcome to wait for.

    Grouped with its neighbour it would inherit that turn's incompleteness, and
    an unrelated row would decide whether the neighbour was reviewable.
    """
    rows = [
        _row("user", "loose row", turn_id=None),
        _row("user", "parked question", turn_id="turn-9"),
    ]

    rendered = _render(rows, completed=())

    assert "loose row" in rendered.text
    assert "parked question" not in rendered.text


def test_an_application_written_user_row_is_not_counted_as_a_person_speaking() -> None:
    rendered = _render([
        _row("user", "System Callback Trigger", is_internal=True),
        _row("assistant", "done"),
    ])

    assert rendered.user_row_count == 0
    assert rendered.user_char_count == 0
    assert "System:" in rendered.text


def test_user_volume_is_measured_for_the_skip_accounting() -> None:
    rendered = _render([
        _row("user", "12345", turn_id="turn-1"),
        _row("assistant", "a long assistant reply that is not the user's text"),
        _row("user", "678", turn_id="turn-1"),
    ])

    assert rendered.user_row_count == 2
    assert rendered.user_char_count == 8


def test_a_sender_label_falls_back_to_user_when_the_row_names_nobody() -> None:
    rendered = _render([_row("user", "hello", user_id=None, internal_id=7)])

    assert rendered.text.startswith("#7 2026-09-17 14:03 User: hello")
