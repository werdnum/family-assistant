"""Turn records: one historical assistant turn, flattened for curation.

A turn is every message-history row sharing a ``turn_id`` in one
conversation, opened by a non-internal ``user`` row. The record keeps what a
curator needs to decide whether the turn becomes a case -- the prompt, the
tool calls with their results, the reply, cost and latency, and the weak
outcome signals the following turn provides -- and nothing is abstracted or
scrubbed: records are household text and stay in the private tree.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from family_assistant.eval.workhorse.strata import (
    intent_bucket,
    template_key,
    tool_family,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from family_assistant.storage.types import MessageHistoryRow

__all__ = [
    "ToolCallRecord",
    "TurnRecord",
    "TurnUsage",
    "build_turn_records",
]

FOLLOWUP_WINDOW = timedelta(minutes=10)
RESULT_CHAR_LIMIT = 2000

# A follow-up that opens like this is read as the user correcting or retrying
# the turn before it. Deliberately narrow: it is a weak label for steering
# sampling towards failures, not a verdict on the turn.
_CORRECTION_PATTERN = re.compile(
    r"^(no[,. ]|not |nope|wrong|actually|i meant|that'?s not|try again|still |"
    r"again|you didn'?t|didn'?t work|what\?)",
    re.IGNORECASE,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCallRecord(_Frozen):
    """One tool call the turn made, paired with its recorded result."""

    name: str
    family: str
    arguments: str
    result: str | None
    result_truncated: bool


class TurnUsage(_Frozen):
    """Summed model usage over the turn's assistant rows."""

    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int
    reasoning_tokens: int
    model_seconds: float
    models: list[str]
    model_tier: str | None
    model_tier_requested: str | None
    model_tier_would_choose: str | None


class TurnRecord(_Frozen):
    """One historical turn, as a curation candidate."""

    turn_id: str
    profile: str
    interface_type: str
    conversation_id: str
    user_id: str | None
    asked_at: datetime
    prompt: str
    attachment_count: int
    population: Literal["human", "templated", "delegated"]
    template: str | None
    intent: str
    previous_turn_id: str | None
    seconds_to_previous_turn: float | None
    tool_calls: list[ToolCallRecord]
    tool_signature: str
    primary_family: str
    families: list[str]
    reply: str | None
    errors: list[str]
    delegated: bool
    usage: TurnUsage
    wall_seconds: float
    followup_within_10m: bool
    correction_shaped_followup: bool
    escalated: bool

    @property
    def negative_signal(self) -> bool:
        """Whether anything recorded suggests the turn went wrong."""
        return bool(self.errors) or self.correction_shaped_followup or self.escalated


def _arguments_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, sort_keys=True, default=str)


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _number(value: object) -> float:
    # Booleans are ints; a duration never is one.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _usage(rows: Sequence[MessageHistoryRow]) -> TurnUsage:
    infos = [
        row["reasoning_info"]
        for row in rows
        if row["role"] == "assistant" and isinstance(row["reasoning_info"], dict)
    ]
    first = infos[0] if infos else {}
    models: list[str] = []
    for info in infos:
        model = info.get("resolved_model")
        if isinstance(model, str) and model not in models:
            models.append(model)
    tier = first.get("model_tier")
    requested = first.get("model_tier_requested")
    would_choose = first.get("model_tier_would_choose")
    return TurnUsage(
        llm_calls=sum(1 for row in rows if row["role"] == "assistant"),
        prompt_tokens=sum(_int(info.get("prompt_tokens")) for info in infos),
        completion_tokens=sum(_int(info.get("completion_tokens")) for info in infos),
        cached_prompt_tokens=sum(
            _int(info.get("cached_prompt_tokens")) for info in infos
        ),
        reasoning_tokens=sum(_int(info.get("reasoning_tokens")) for info in infos),
        model_seconds=sum(_number(info.get("duration_ms")) for info in infos) / 1000,
        models=models,
        model_tier=tier if isinstance(tier, str) else None,
        model_tier_requested=requested if isinstance(requested, str) else None,
        model_tier_would_choose=(
            would_choose if isinstance(would_choose, str) else None
        ),
    )


def _tool_calls(rows: Sequence[MessageHistoryRow]) -> list[ToolCallRecord]:
    results: dict[str, str] = {
        row["tool_call_id"]: row["content"] or ""
        for row in rows
        if row["role"] == "tool" and row["tool_call_id"]
    }
    calls: list[ToolCallRecord] = []
    for row in rows:
        if row["role"] != "assistant":
            continue
        for item in row["tool_calls"] or []:
            result = results.get(item.id)
            truncated = result is not None and len(result) > RESULT_CHAR_LIMIT
            if result is not None and truncated:
                result = result[:RESULT_CHAR_LIMIT]
            calls.append(
                ToolCallRecord(
                    name=item.function.name,
                    family=tool_family(item.function.name),
                    arguments=_arguments_text(item.function.arguments),
                    result=result,
                    result_truncated=truncated,
                )
            )
    return calls


def _reply(rows: Sequence[MessageHistoryRow]) -> str | None:
    for row in reversed(rows):
        if row["role"] == "assistant" and row["content"] and not row["tool_calls"]:
            return row["content"]
    return None


def _errors(rows: Sequence[MessageHistoryRow]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        if row["role"] == "error":
            errors.append(row["content"] or "(error row without content)")
        elif row["error_traceback"]:
            errors.append(row["error_traceback"].strip().splitlines()[-1])
    return errors


def _primary_family(calls: Sequence[ToolCallRecord]) -> str:
    """The family the turn called most, ignoring tool-discovery bookkeeping."""
    counts = Counter(call.family for call in calls if call.family != "meta")
    if not counts:
        return "none"
    return min(counts, key=lambda family: (-counts[family], family))


@dataclass(frozen=True, slots=True, eq=False)
class _Opening:
    """A turn's opening user row and the rows that make up the turn."""

    user_row: MessageHistoryRow
    rows: list[MessageHistoryRow]


def _openings(
    conversation_rows: Sequence[MessageHistoryRow],
) -> list[_Opening]:
    """Split a conversation into turns, in the order they were asked."""
    by_turn: dict[str, list[MessageHistoryRow]] = {}
    for row in conversation_rows:
        if row["turn_id"]:
            by_turn.setdefault(row["turn_id"], []).append(row)
    openings: list[_Opening] = []
    for rows in by_turn.values():
        user_row = next((row for row in rows if row["role"] == "user"), None)
        if user_row is not None and user_row["content"]:
            openings.append(_Opening(user_row, rows))
    openings.sort(
        key=lambda opening: (
            opening.user_row["timestamp"],
            opening.user_row["internal_id"],
        )
    )
    return openings


def build_turn_records(
    grouped: Mapping[tuple[str, str], list[MessageHistoryRow]],
    *,
    profiles: Iterable[str],
    templated_prefixes: Sequence[str],
) -> list[TurnRecord]:
    """Flatten grouped history into one record per turn of the named profiles.

    A conversation also holds turns no person asked: a delegated profile's
    run and a scheduled automation share the thread, and a delegation to
    ``complex_tasks`` runs on an extracted profile. Outcome signals and the
    preceding turn therefore read only *human* turns of the named profiles,
    so a delegation to ``media_analyst`` seconds later is not mistaken for the
    user coming back, while re-asking on ``complex_tasks`` after
    ``default_assistant`` still counts as escalation.
    """
    wanted = set(profiles)
    records: list[TurnRecord] = []
    for (interface_type, conversation_id), conversation_rows in grouped.items():
        openings = [
            opening
            for opening in _openings(conversation_rows)
            if opening.user_row["processing_profile_id"] in wanted
        ]
        human = [
            opening
            for opening in openings
            if _population(opening.user_row, templated_prefixes) == "human"
        ]
        for opening in openings:
            asked_at = opening.user_row["timestamp"]
            profile = opening.user_row["processing_profile_id"]
            earlier = [
                other
                for other in human
                if other is not opening
                and other.user_row["timestamp"] <= asked_at
                and other.user_row["processing_profile_id"] == profile
            ]
            later = [
                other
                for other in human
                if other is not opening and other.user_row["timestamp"] > asked_at
            ]
            records.append(
                _record(
                    opening,
                    previous=earlier[-1] if earlier else None,
                    following=later[0] if later else None,
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    templated_prefixes=templated_prefixes,
                )
            )
    records.sort(key=lambda record: (record.asked_at, record.turn_id))
    return records


def _population(
    user_row: MessageHistoryRow, templated_prefixes: Sequence[str]
) -> Literal["human", "templated", "delegated"]:
    """Who wrote the prompt: a person, a scheduled automation, or a model.

    Automations run in subconversations and some open with an internal row,
    so the template is checked first. Of the rest, a delegated run's opening
    row (the delegating model's request) carries a ``subconversation_id``,
    and the wake that hands a delegation's result back opens with an
    internal row; both belong to the delegation, not to a person.
    """
    if template_key(user_row["content"] or "", templated_prefixes) is not None:
        return "templated"
    if user_row["subconversation_id"] is not None or user_row["is_internal"]:
        return "delegated"
    return "human"


def _record(
    opening: _Opening,
    *,
    previous: _Opening | None,
    following: _Opening | None,
    interface_type: str,
    conversation_id: str,
    templated_prefixes: Sequence[str],
) -> TurnRecord:
    user_row = opening.user_row
    rows = opening.rows
    prompt = user_row["content"] or ""
    profile = user_row["processing_profile_id"] or "(none)"
    asked_at = user_row["timestamp"]
    template = template_key(prompt, templated_prefixes)
    calls = _tool_calls(rows)
    families = sorted({call.family for call in calls})

    followup = following is not None and (
        following.user_row["timestamp"] - asked_at <= FOLLOWUP_WINDOW
    )
    correction = bool(
        followup
        and following is not None
        and _CORRECTION_PATTERN.match((following.user_row["content"] or "").strip())
    )
    escalated = bool(
        followup
        and following is not None
        and following.user_row["processing_profile_id"] != profile
    )

    return TurnRecord(
        turn_id=user_row["turn_id"] or "",
        profile=profile,
        interface_type=interface_type,
        conversation_id=conversation_id,
        user_id=user_row["user_id"],
        asked_at=asked_at,
        prompt=prompt,
        attachment_count=len(user_row["attachments"] or []),
        population=_population(user_row, templated_prefixes),
        template=template,
        intent=intent_bucket(prompt),
        previous_turn_id=previous.user_row["turn_id"] if previous else None,
        seconds_to_previous_turn=(
            (asked_at - previous.user_row["timestamp"]).total_seconds()
            if previous
            else None
        ),
        tool_calls=calls,
        tool_signature="+".join(sorted({call.name for call in calls})) or "(none)",
        primary_family=_primary_family(calls),
        families=families,
        reply=_reply(rows),
        errors=_errors(rows),
        delegated=any(call.name == "delegate_to_service" for call in calls),
        usage=_usage(rows),
        wall_seconds=(max(row["timestamp"] for row in rows) - asked_at).total_seconds(),
        followup_within_10m=followup,
        correction_shaped_followup=correction,
        escalated=escalated,
    )
