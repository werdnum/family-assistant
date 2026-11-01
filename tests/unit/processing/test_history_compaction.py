"""Compaction planning and the compacted rendering of a turn."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from family_assistant.llm.deferred_tools import activated_tool_names
from family_assistant.llm.messages import (
    AssistantMessage,
    ImageUrlContentPart,
    LLMMessage,
    MessageWithMetadata,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing.history_compaction import (
    CompactionCandidate,
    CompactionCapabilities,
    can_compact,
    plan_compaction,
    render_compacted_turn,
)
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    prompt_window_taint,
)
from family_assistant.storage.history_compaction import TurnDecision, TurnMode

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
VERBATIM = TurnDecision(TurnMode.VERBATIM)


def _candidate(
    key: str,
    *,
    verbatim: int = 1_000,
    compacted: int | None = 100,
    age: timedelta = timedelta(minutes=5),
    previous: TurnDecision | None = VERBATIM,
    explicit: bool = False,
) -> CompactionCandidate:
    return CompactionCandidate(
        key=key,
        last_activity=NOW - age,
        verbatim_size=verbatim,
        compacted_size=compacted,
        previous=previous,
        explicit=explicit,
    )


def _plan(
    candidates: list[CompactionCandidate], *, target: int, min_turns: int = 1
) -> dict[str, TurnMode]:
    plan = plan_compaction(
        candidates,
        target_chars=target,
        min_turns=min_turns,
        cutoff=NOW - timedelta(hours=1),
        boundary_internal_id=100,
    )
    return {key: decision.mode for key, decision in plan.decisions.items()}


def test_a_window_within_the_target_is_unchanged() -> None:
    plan = plan_compaction(
        [_candidate("a"), _candidate("b")],
        target_chars=5_000,
        min_turns=1,
        cutoff=NOW - timedelta(hours=1),
        boundary_internal_id=100,
    )

    assert not plan.changed
    assert set(plan.decisions) == {"a", "b"}


def test_older_turns_are_compacted_before_any_is_dropped() -> None:
    modes = _plan([_candidate("a"), _candidate("b"), _candidate("c")], target=1_300)

    assert modes == {
        "a": TurnMode.COMPACTED,
        "b": TurnMode.COMPACTED,
        "c": TurnMode.VERBATIM,
    }


def test_a_turn_that_cannot_be_compacted_is_dropped_whole() -> None:
    modes = _plan([_candidate("a", compacted=None), _candidate("b")], target=1_000)

    assert modes == {"b": TurnMode.VERBATIM}


def test_the_newest_turns_are_compacted_but_never_dropped() -> None:
    modes = _plan(
        [_candidate("a", compacted=None), _candidate("b", verbatim=5_000)],
        target=200,
    )

    assert modes == {"b": TurnMode.COMPACTED}


def test_a_minimum_larger_than_the_window_keeps_every_turn() -> None:
    modes = _plan(
        [_candidate("a", compacted=None), _candidate("b", compacted=None)],
        target=0,
        min_turns=4,
    )

    assert set(modes) == {"a", "b"}


def test_aged_turns_leave_unless_explicitly_referenced() -> None:
    modes = _plan(
        [
            _candidate("old", age=timedelta(hours=2)),
            _candidate("pinned", age=timedelta(hours=2), explicit=True),
            _candidate("new"),
        ],
        target=10_000,
    )

    assert set(modes) == {"pinned", "new"}


def test_thinking_is_stripped_after_the_first_changed_turn_only() -> None:
    plan = plan_compaction(
        [
            _candidate("kept-before", previous=TurnDecision(TurnMode.COMPACTED)),
            _candidate("changed"),
            _candidate("after"),
        ],
        target_chars=1_200,
        min_turns=1,
        cutoff=NOW - timedelta(hours=1),
        boundary_internal_id=100,
    )

    assert plan.decisions["kept-before"].strip_through is None
    assert plan.decisions["changed"].mode is TurnMode.COMPACTED
    assert plan.decisions["after"] == TurnDecision(TurnMode.VERBATIM, strip_through=100)


def test_an_earlier_strip_holds_at_later_events() -> None:
    plan = plan_compaction(
        [_candidate("a", previous=TurnDecision(TurnMode.VERBATIM, strip_through=7))],
        target_chars=10_000,
        min_turns=1,
        cutoff=NOW - timedelta(hours=1),
        boundary_internal_id=100,
    )

    assert plan.decisions["a"].strip_through == 7


def _row(index: int, message: LLMMessage) -> MessageWithMetadata:
    return MessageWithMetadata(
        message=message,
        internal_id=str(index),
        interface_message_id=None,
        timestamp=NOW + timedelta(seconds=index),
        conversation_id="c",
        interface_type="telegram",
        turn_id="t",
    )


def _call(call_id: str, name: str) -> AssistantMessage:
    return AssistantMessage(
        tool_calls=[
            ToolCallItem(
                id=call_id,
                type="function",
                function=ToolCallFunction(name=name, arguments="{}"),
            )
        ],
        provider_metadata={"provider": "anthropic", "thinking_blocks": []},
    )


def _external_taint() -> TaintMetadata:
    return (
        TurnTaintState
        .empty()
        .add_source(
            TaintSource(
                source_type=TaintSourceType.TOOL_OUTPUT,
                source_id="web",
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="fetched a web page",
            )
        )
        .to_metadata()
    )


def _tool_turn() -> list[MessageWithMetadata]:
    return [
        _row(
            0,
            UserMessage(
                content=[
                    TextContentPart(type="text", text="Look this up"),
                    ImageUrlContentPart(
                        type="image_url",
                        image_url={"url": "/api/attachments/att-1"},
                        attachment_id="att-1",
                    ),
                ]
            ),
        ),
        _row(1, _call("c1", "activate_tools")),
        _row(
            2,
            ToolMessage(
                tool_call_id="c1",
                name="activate_tools",
                content="Activated web_fetch",
                activated_tools=["web_fetch"],
            ),
        ),
        _row(3, _call("c2", "web_fetch")),
        _row(
            4,
            ToolMessage(
                tool_call_id="c2",
                name="web_fetch",
                content="x" * 5_000,
                taint_metadata=_external_taint(),
            ),
        ),
        _row(5, AssistantMessage(content="Here is what I found.")),
    ]


def test_a_compacted_turn_keeps_the_request_stubs_and_answer() -> None:
    rendered = render_compacted_turn(_tool_turn(), ZoneInfo("UTC"))

    final = rendered[-1]
    assert isinstance(final, AssistantMessage)
    assert final.content is not None
    assert final.content.startswith(
        '[Tool calls at 2026-10-05 12:00 compacted; get_message_history(turn_id="t")'
    )
    assert "[Called web_fetch]" in final.content
    assert final.content.endswith("Here is what I found.")
    user = rendered[0]
    assert isinstance(user, UserMessage)
    assert isinstance(user.content, list)
    assert not any(isinstance(part, ImageUrlContentPart) for part in user.content)
    assert any(
        isinstance(part, TextContentPart) and "att-1" in part.text
        for part in user.content
    )
    assert not any(
        isinstance(message, ToolMessage) and message.content == "x" * 5_000
        for message in rendered
    )
    assert all(
        message.provider_metadata is None
        for message in rendered
        if isinstance(message, AssistantMessage)
    )


def test_compacting_a_turn_ends_its_activations() -> None:
    """The instructions that came with a tool are gone, so the tool goes too."""
    turn = _tool_turn()
    assert activated_tool_names([row.message for row in turn])

    rendered = render_compacted_turn(turn, ZoneInfo("UTC"))

    assert not activated_tool_names(rendered)


def test_compacting_a_turn_does_not_reduce_its_taint() -> None:
    turn = _tool_turn()

    verbatim = prompt_window_taint([row.message for row in turn])
    compacted = prompt_window_taint(render_compacted_turn(turn, ZoneInfo("UTC")))

    assert compacted.max_tier >= verbatim.max_tier
    assert {source.source_id for source in verbatim.sources} <= {
        source.source_id for source in compacted.sources
    }


def _write_turn(*, turn_id: str | None = "t") -> list[MessageWithMetadata]:
    rows = [
        _row(0, UserMessage(content="Remind me about the plumber, and check notes")),
        _row(
            1,
            AssistantMessage(
                tool_calls=[
                    ToolCallItem(
                        id=call_id,
                        type="function",
                        function=ToolCallFunction(name=name, arguments=arguments),
                    )
                    for call_id, name, arguments in (
                        ("r1", "search_notes", '{"query": "plumber"}'),
                        ("r2", "search_notes", '{"query": "quote"}'),
                        ("w1", "schedule_reminder", '{"message": "Call plumber"}'),
                        ("w2", "send_message_to_user", '{"target_chat_id": "9"}'),
                    )
                ],
            ),
        ),
        _row(2, ToolMessage(tool_call_id="r1", name="search_notes", content="a")),
        _row(3, ToolMessage(tool_call_id="r2", name="search_notes", content="b")),
        _row(
            4,
            ToolMessage(
                tool_call_id="w1",
                name="schedule_reminder",
                content="Reminder task_42 scheduled for Fri 15:00\nmore detail",
            ),
        ),
        _row(
            5,
            ToolMessage(
                tool_call_id="w2",
                name="send_message_to_user",
                content="Error: Could not send",
                error_traceback="Traceback ...",
            ),
        ),
        _row(6, AssistantMessage(content="Reminder set.")),
    ]
    if turn_id is None:
        return [
            MessageWithMetadata(
                message=row.message,
                internal_id=row.internal_id,
                interface_message_id=None,
                timestamp=row.timestamp,
                conversation_id=row.conversation_id,
                interface_type=row.interface_type,
            )
            for row in rows
        ]
    return rows


def _compacted_text(rows: list[MessageWithMetadata]) -> str:
    rendered = render_compacted_turn(
        rows,
        ZoneInfo("UTC"),
        frozenset({"schedule_reminder", "send_message_to_user"}),
    )
    final = rendered[-1]
    assert isinstance(final, AssistantMessage)
    assert final.content is not None
    return final.content


def test_a_compacted_turn_keeps_each_consequential_outcome() -> None:
    lines = _compacted_text(_write_turn()).splitlines()

    assert lines == [
        '[Tool calls at 2026-10-05 12:00 compacted; get_message_history(turn_id="t") '
        "retrieves the calls and results]",
        "[Called search_notes 2 times]",
        '[Called schedule_reminder (call w1) with {"message": "Call plumber"}: '
        "Reminder task_42 scheduled for Fri 15:00 more detail]",
        '[Called send_message_to_user (call w2) with {"target_chat_id": "9"}: '
        "failed: Error: Could not send]",
        "Reminder set.",
    ]


def test_a_compacted_turn_without_a_turn_id_names_its_calls() -> None:
    text = _compacted_text(_write_turn(turn_id=None))

    assert (
        'get_message_history(tool_call_ids=["r1", "r2", "w1", "w2"]) retrieves' in text
    )


def test_a_consequential_outcome_line_is_bounded() -> None:
    rows = _write_turn()
    rows[4] = _row(
        4,
        ToolMessage(tool_call_id="w1", name="schedule_reminder", content="y" * 5_000),
    )

    line = next(
        line for line in _compacted_text(rows).splitlines() if "(call w1)" in line
    )

    assert len(line) < 600


def test_an_image_turn_compacts_only_where_a_model_can_look_again() -> None:
    turn = _tool_turn()

    assert not can_compact(
        turn, CompactionCapabilities(history_tool=True, media_tool=False)
    )
    assert can_compact(turn, CompactionCapabilities(history_tool=True, media_tool=True))


def test_a_strip_survives_compaction_and_a_later_restore() -> None:
    stripped_then_compacted = TurnDecision(TurnMode.COMPACTED, strip_through=7)

    plan = plan_compaction(
        [_candidate("a", previous=stripped_then_compacted)],
        target_chars=10_000,
        min_turns=1,
        cutoff=NOW - timedelta(hours=1),
        boundary_internal_id=100,
    )

    assert plan.decisions["a"] == TurnDecision(TurnMode.VERBATIM, strip_through=7)


def test_a_turn_coming_back_into_the_window_loses_its_thinking() -> None:
    plan = plan_compaction(
        [_candidate("pinned", previous=None, explicit=True)],
        target_chars=10_000,
        min_turns=1,
        cutoff=NOW - timedelta(hours=1),
        boundary_internal_id=100,
    )

    assert plan.decisions["pinned"].strip_through == 100


def test_an_image_without_an_id_is_never_compacted() -> None:
    turn = [
        _row(
            0,
            UserMessage(
                content=[
                    ImageUrlContentPart(
                        type="image_url", image_url={"url": "data:image/png;base64,"}
                    )
                ]
            ),
        ),
        _row(1, AssistantMessage(content="A cat.")),
    ]

    assert not can_compact(
        turn, CompactionCapabilities(history_tool=True, media_tool=True)
    )
