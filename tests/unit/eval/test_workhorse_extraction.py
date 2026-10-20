"""Tests for the workhorse eval's turn-record export and candidate sampling."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import yaml

from family_assistant.eval import private_paths
from family_assistant.eval.workhorse.strata import (
    DEFAULT_TEMPLATED_PREFIXES,
    allocate_picks,
    intent_bucket,
    sample_candidates,
    size_bucket,
    stratum_frequencies,
    stratum_of,
    template_key,
    tool_family,
)
from family_assistant.eval.workhorse.turn_records import (
    TurnRecord,
    TurnUsage,
    build_turn_records,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.paths import PROJECT_ROOT
from family_assistant.storage.types import MessageHistoryRow

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from family_assistant.llm.messages import MessageReasoningInfo

pytestmark = pytest.mark.no_db

_T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


class _Conversation:
    """Builds the rows of one conversation in chronological order."""

    def __init__(self, conversation_id: str = "chat-1") -> None:
        self.conversation_id = conversation_id
        self.rows: list[MessageHistoryRow] = []

    def add(
        self,
        *,
        turn_id: str,
        role: str,
        at: datetime,
        profile: str = "default_assistant",
        content: str | None = None,
        tool_calls: list[ToolCallItem] | None = None,
        tool_call_id: str | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        error_traceback: str | None = None,
        subconversation_id: str | None = None,
    ) -> None:
        self.rows.append(
            MessageHistoryRow(
                internal_id=len(self.rows) + 1,
                interface_type="telegram",
                conversation_id=self.conversation_id,
                interface_message_id=None,
                turn_id=turn_id,
                thread_root_id=None,
                timestamp=at,
                role=role,
                content=content,
                tool_calls=tool_calls,
                reasoning_info=reasoning_info,
                tool_call_id=tool_call_id,
                error_traceback=error_traceback,
                processing_profile_id=profile,
                subconversation_id=subconversation_id,
                user_id="andrew",
                attachments=None,
                tool_name=None,
                provider_metadata=None,
                taint_metadata_json=None,
                taint_metadata_version=None,
                taint_metadata=None,
                is_internal=False,
                echoed_from_conversation_id=None,
                moved_to_conversation_id=None,
            )
        )

    def simple_turn(
        self,
        turn_id: str,
        prompt: str,
        at: datetime,
        *,
        profile: str = "default_assistant",
        tools: tuple[str, ...] = (),
    ) -> None:
        self.add(turn_id=turn_id, role="user", at=at, profile=profile, content=prompt)
        calls = [
            ToolCallItem(
                id=f"{turn_id}-{index}",
                type="function",
                function=ToolCallFunction(name=name, arguments={"q": index}),
            )
            for index, name in enumerate(tools)
        ]
        if calls:
            self.add(
                turn_id=turn_id,
                role="assistant",
                at=at + timedelta(seconds=1),
                profile=profile,
                tool_calls=calls,
                reasoning_info={
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "duration_ms": 1499.5,
                    "resolved_model": "model-a",
                    "model_tier": "standard",
                },
            )
            for call in calls:
                self.add(
                    turn_id=turn_id,
                    role="tool",
                    at=at + timedelta(seconds=2),
                    profile=profile,
                    content=f"result of {call.function.name}",
                    tool_call_id=call.id,
                )
        self.add(
            turn_id=turn_id,
            role="assistant",
            at=at + timedelta(seconds=3),
            profile=profile,
            content=f"reply to {prompt}",
            reasoning_info={"prompt_tokens": 200, "duration_ms": 500},
        )

    def grouped(self) -> dict[tuple[str, str], list[MessageHistoryRow]]:
        return {("telegram", self.conversation_id): self.rows}


def _records(conversation: _Conversation) -> dict[str, TurnRecord]:
    records = build_turn_records(
        conversation.grouped(),
        profiles=("default_assistant", "complex_tasks"),
        templated_prefixes=DEFAULT_TEMPLATED_PREFIXES,
    )
    return {record.turn_id: record for record in records}


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("get_note", "notes"),
        ("list_notes", "notes"),
        ("add_or_update_note", "notes"),
        ("search_calendar_events", "calendar"),
        ("schedule_future_callback", "scheduling"),
        ("ha_call_read_tool", "home"),
        ("render_home_assistant_template", "home"),
        ("brave_web_search", "web"),
        ("google_shopping_search", "web"),
        ("gmail_search", "google"),
        ("execute_python", "code_data"),
        ("query_trino", "code_data"),
        ("delegate_to_service", "delegation"),
        ("activate_tools", "meta"),
        ("plan_trip", "travel_weather"),
        ("tuit_checkpoint", "tasks"),
        ("search_documents", "documents"),
        ("get_message_history", "history"),
        ("something_new", "other"),
    ],
)
def test_tool_family(name: str, family: str) -> None:
    assert tool_family(name) == family


@pytest.mark.parametrize(
    ("prompt", "bucket"),
    [
        ("Remind me to call the vet at 5", "reminder"),
        ("Can you add the dentist\nto the calendar for Tuesday", "calendar write"),
        ("What's on tomorrow?", "agenda lookup"),
        ("Turn off the garage lights", "smart home"),
        ("Thanks!", "other"),
    ],
)
def test_intent_bucket(prompt: str, bucket: str) -> None:
    assert intent_bucket(prompt) == bucket


def test_template_key_matches_only_a_leading_prefix() -> None:
    prefixes = DEFAULT_TEMPLATED_PREFIXES
    assert template_key("  System Callback Trigger: x", prefixes) == (
        "System Callback Trigger"
    )
    assert template_key("Please Generate an e-ink display", prefixes) is None


@pytest.mark.parametrize(
    ("count", "bucket"),
    [(0, "0"), (1, "1-3"), (3, "1-3"), (4, "4-7"), (15, "8-15"), (16, "16+")],
)
def test_size_bucket(count: int, bucket: str) -> None:
    assert size_bucket(count) == bucket


def test_turn_record_pairs_calls_with_results_and_sums_usage() -> None:
    conversation = _Conversation()
    conversation.simple_turn(
        "t1", "What's on tomorrow?", _T0, tools=("search_calendar_events", "get_note")
    )

    record = _records(conversation)["t1"]

    assert record.population == "human"
    assert record.intent == "agenda lookup"
    assert [call.name for call in record.tool_calls] == [
        "search_calendar_events",
        "get_note",
    ]
    assert record.tool_calls[0].result == "result of search_calendar_events"
    assert record.tool_signature == "get_note+search_calendar_events"
    assert record.primary_family == "calendar"
    assert record.reply == "reply to What's on tomorrow?"
    assert record.usage.llm_calls == 2
    assert record.usage.prompt_tokens == 300
    assert record.usage.model_seconds == pytest.approx(1.9995)
    assert record.usage.models == ["model-a"]
    assert record.usage.model_tier == "standard"
    assert record.wall_seconds == 3.0
    assert not record.negative_signal


def test_delegated_and_templated_turns_are_not_read_as_the_user_coming_back() -> None:
    """A delegated run or an automation seconds later is not a follow-up."""
    conversation = _Conversation()
    conversation.simple_turn(
        "t1", "Describe this photo", _T0, tools=("delegate_to_service",)
    )
    conversation.simple_turn(
        "sub",
        "Inspect the attached image",
        _T0 + timedelta(seconds=5),
        profile="media_analyst",
    )
    conversation.simple_turn(
        "auto", "System Callback Trigger: check", _T0 + timedelta(minutes=1)
    )

    records = _records(conversation)

    assert set(records) == {"t1", "auto"}
    assert records["auto"].population == "templated"
    assert records["t1"].delegated
    assert not records["t1"].followup_within_10m
    assert not records["t1"].escalated


def test_a_delegation_to_complex_tasks_is_not_escalation() -> None:
    """The delegated run is on an extracted profile but no person wrote it."""
    conversation = _Conversation()
    conversation.simple_turn("t1", "Research this", _T0, tools=("delegate_to_service",))
    conversation.add(
        turn_id="sub",
        role="user",
        at=_T0 + timedelta(seconds=5),
        profile="complex_tasks",
        content="Andrew wants this researched in depth",
        subconversation_id="delegation-1",
    )

    records = _records(conversation)

    assert records["sub"].population == "delegated"
    assert not records["t1"].followup_within_10m
    assert not records["t1"].escalated


def test_an_automation_in_a_subconversation_is_templated_not_delegated() -> None:
    conversation = _Conversation()
    conversation.add(
        turn_id="auto",
        role="user",
        at=_T0,
        content="Generate an e-ink display for today",
        subconversation_id="thread-1",
    )

    assert _records(conversation)["auto"].population == "templated"


def test_re_asking_on_complex_tasks_marks_escalation_and_links_context() -> None:
    conversation = _Conversation()
    conversation.simple_turn("t1", "Plan the weekend", _T0)
    conversation.simple_turn(
        "t2",
        "Plan the weekend properly",
        _T0 + timedelta(minutes=3),
        profile="complex_tasks",
    )
    conversation.simple_turn("t3", "No, that's wrong", _T0 + timedelta(minutes=5))
    conversation.simple_turn("t4", "Thanks", _T0 + timedelta(hours=2))

    records = _records(conversation)

    assert records["t1"].escalated
    assert records["t1"].followup_within_10m
    assert records["t2"].correction_shaped_followup
    assert not records["t3"].followup_within_10m
    # The preceding turn is on the same profile: history is partitioned by it.
    assert records["t3"].previous_turn_id == "t1"
    assert records["t2"].previous_turn_id is None


def test_error_rows_and_tracebacks_are_negative_signals() -> None:
    conversation = _Conversation()
    conversation.add(turn_id="t1", role="user", at=_T0, content="Do the thing")
    conversation.add(
        turn_id="t1",
        role="error",
        at=_T0 + timedelta(seconds=1),
        content="I encountered an error",
        error_traceback="Traceback...\nValueError: boom",
    )

    record = _records(conversation)["t1"]

    assert record.errors == ["I encountered an error"]
    assert record.negative_signal


def test_allocate_gives_each_stratum_one_pick_then_sqrt_shares() -> None:
    allocation = allocate_picks({"big": 400, "mid": 100, "small": 1}, target=13)

    assert allocation["small"] == 1
    # The rest follow sqrt(size): 20 to 10, so twice the picks.
    assert allocation["big"] == 8
    assert allocation["mid"] == 4


def test_allocate_with_more_strata_than_picks_covers_the_largest() -> None:
    allocation = allocate_picks({"a": 5, "b": 9, "c": 1}, target=2)

    assert allocation == {"b": 1, "a": 1, "c": 0}


def _synthetic_records(count: int, family_tools: tuple[str, ...]) -> list[TurnRecord]:
    conversation = _Conversation()
    for index in range(count):
        tools = (family_tools[index % len(family_tools)],) * (index % 9)
        conversation.simple_turn(
            f"t{index:03}", f"prompt {index}", _T0 + timedelta(hours=index), tools=tools
        )
    return list(_records(conversation).values())


def test_sample_is_deterministic_weighted_and_spread() -> None:
    records = _synthetic_records(90, ("get_note", "gmail_search", "ha_search"))

    first = sample_candidates(
        records, target=9, templated_per_template=2, negative_share=1 / 3, seed="s"
    )
    second = sample_candidates(
        list(reversed(records)),
        target=9,
        templated_per_template=2,
        negative_share=1 / 3,
        seed="s",
    )
    other_seed = sample_candidates(
        records, target=9, templated_per_template=2, negative_share=1 / 3, seed="t"
    )

    assert first == second
    assert first != other_seed
    assert len(first) == 9
    by_id = {record.turn_id: record for record in records}
    frequencies = stratum_frequencies(records)
    # Weights sum back to each sampled stratum's population.
    for stratum, population in frequencies.items():
        picks = [pick for pick in first if pick.stratum == stratum]
        if picks:
            assert sum(pick.weight for pick in picks) == pytest.approx(population)
    sizes = {len(by_id[pick.turn_id].tool_calls) for pick in first}
    assert len(sizes) > 1


def test_sample_prefers_negative_turns_up_to_the_share() -> None:
    conversation = _Conversation()
    for index in range(30):
        at = _T0 + timedelta(hours=index)
        conversation.add(turn_id=f"t{index:02}", role="user", at=at, content="Hi")
        if index < 5:
            conversation.add(
                turn_id=f"t{index:02}",
                role="error",
                at=at + timedelta(seconds=1),
                content="failed",
            )
    records = list(_records(conversation).values())

    sampled = sample_candidates(
        records, target=6, templated_per_template=0, negative_share=0.5, seed="s"
    )

    assert len(sampled) == 6
    assert sum(pick.negative_signal for pick in sampled) == 3


def test_oversampled_negatives_are_weighted_back_to_their_share() -> None:
    """Three picks from five failures must not read as half the workload."""
    conversation = _Conversation()
    for index in range(30):
        at = _T0 + timedelta(hours=index)
        conversation.add(turn_id=f"t{index:02}", role="user", at=at, content="Hi")
        if index < 5:
            conversation.add(
                turn_id=f"t{index:02}",
                role="error",
                at=at + timedelta(seconds=1),
                content="failed",
            )
    records = list(_records(conversation).values())

    sampled = sample_candidates(
        records, target=6, templated_per_template=0, negative_share=0.5, seed="s"
    )

    negative_weight = sum(pick.weight for pick in sampled if pick.negative_signal)
    assert negative_weight == pytest.approx(5)
    assert sum(pick.weight for pick in sampled) == pytest.approx(30)


def test_delegated_runs_are_recorded_but_not_sampled() -> None:
    conversation = _Conversation()
    conversation.add(
        turn_id="sub",
        role="user",
        at=_T0,
        profile="complex_tasks",
        content="Andrew wants this researched",
        subconversation_id="delegation-1",
    )
    records = list(_records(conversation).values())

    sampled = sample_candidates(
        records, target=5, templated_per_template=2, negative_share=0, seed="s"
    )

    assert stratum_frequencies(records) == {"delegated/complex_tasks/none": 1}
    assert sampled == []


def test_templated_turns_contribute_a_fixed_handful_per_template() -> None:
    conversation = _Conversation()
    for index in range(20):
        conversation.simple_turn(
            f"e{index:02}",
            "Generate an e-ink display for today",
            _T0 + timedelta(hours=index),
        )
    records = list(_records(conversation).values())

    sampled = sample_candidates(
        records, target=10, templated_per_template=2, negative_share=0, seed="s"
    )

    assert (
        {pick.stratum for pick in sampled}
        == {stratum_of(records[0]).key}
        == {"templated/default_assistant/Generate an e-ink display"}
    )
    assert len(sampled) == 2
    assert sampled[0].weight == 10


def _load_script() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts" / "extract_workhorse_turns.py"
    spec = importlib.util.spec_from_file_location(
        "extract_workhorse_turns_script", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_refuses_a_destination_outside_the_private_tree() -> None:
    script = _load_script()
    with pytest.raises(SystemExit, match="private tree"):
        script.main(["--out-dir", "docs/turns"])


def test_cli_refuses_a_non_empty_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", tmp_path)
    out_dir = tmp_path / ".review-eval-local" / "turns"
    out_dir.mkdir(parents=True)
    (out_dir / "candidates.yaml").write_text("[]", encoding="utf-8")
    script = _load_script()

    with pytest.raises(SystemExit, match="non-empty"):
        script.main(["--out-dir", ".review-eval-local/turns"])


def test_cli_writes_records_summary_and_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".review-eval-local").mkdir()
    conversation = _Conversation()
    conversation.simple_turn("t1", "What's on tomorrow?", _T0, tools=("get_note",))
    conversation.simple_turn(
        "t2", "System Callback Trigger: x", _T0 + timedelta(hours=1)
    )
    script = _load_script()

    async def fake_read_history(
        database_url: str, *, since: datetime | None
    ) -> dict[tuple[str, str], list[MessageHistoryRow]]:
        del database_url, since
        return conversation.grouped()

    monkeypatch.setattr(script, "_read_history", fake_read_history)

    assert script.main(["--out-dir", ".review-eval-local/turns"]) == 0

    out_dir = tmp_path / ".review-eval-local" / "turns"
    lines = (out_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
    assert [TurnRecord.model_validate_json(line).turn_id for line in lines] == [
        "t1",
        "t2",
    ]
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["turns_by_population"] == {"human": 1, "templated": 1}
    assert summary["stratum_frequencies"] == {
        "human/default_assistant/notes": 1,
        "templated/default_assistant/System Callback Trigger": 1,
    }
    candidates = yaml.safe_load((out_dir / "candidates.yaml").read_text("utf-8"))
    assert {entry["turn_id"] for entry in candidates} == {"t1", "t2"}
    assert all(entry["decision"] is None for entry in candidates)


def test_turn_usage_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        TurnUsage.model_validate({
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_prompt_tokens": 0,
            "reasoning_tokens": 0,
            "model_seconds": 0,
            "models": [],
            "model_tier": None,
            "model_tier_requested": None,
            "model_tier_would_choose": None,
            "surprise": 1,
        })
