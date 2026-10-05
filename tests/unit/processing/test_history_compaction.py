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
    assert "[Called web_fetch at 2026-10-05 12:00; get_message_history" in final.content
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


def test_compacting_a_turn_keeps_its_activations() -> None:
    turn = _tool_turn()

    rendered = render_compacted_turn(turn, ZoneInfo("UTC"))

    assert activated_tool_names(rendered) == activated_tool_names([
        row.message for row in turn
    ])


def test_compacting_a_turn_does_not_reduce_its_taint() -> None:
    turn = _tool_turn()

    verbatim = prompt_window_taint([row.message for row in turn])
    compacted = prompt_window_taint(render_compacted_turn(turn, ZoneInfo("UTC")))

    assert compacted.max_tier >= verbatim.max_tier
    assert {source.source_id for source in verbatim.sources} <= {
        source.source_id for source in compacted.sources
    }


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
