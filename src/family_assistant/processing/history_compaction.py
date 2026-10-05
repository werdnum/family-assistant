"""Compaction events: what each turn in the history window becomes.

Between events the window only appends. At an event, every completed turn up to
it is decided once -- verbatim, compacted, or gone -- and the decision is
recorded, so later requests render those turns identically from stored rows.
See docs/design/history-compaction.md.

This module is pure: the plan and the compacted rendering are functions of
their inputs, so a replay is byte-identical without persisting any text.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from family_assistant.llm.messages import (
    AssistantMessage,
    AttachmentContentPart,
    ErrorMessage,
    ImageUrlContentPart,
    LLMMessage,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing.message_time import with_sent_at
from family_assistant.security.taint import introduced_taint_metadata
from family_assistant.storage.history_compaction import TurnDecision, TurnMode

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from family_assistant.llm.messages import ContentPart, MessageWithMetadata

# A compaction event brings the window down to this share of its budget, so the
# next budget event is many turns away rather than one.
COMPACTION_TARGET_RATIO = 0.5

HISTORY_TOOL_NAME = "get_message_history"
# Tools that can look at an image again from its id. Only a delegation passing
# attachment_ids reaches a model that reads media; read_text_attachment decodes
# text and get_attachment_info returns metadata, so neither recovers an image.
MEDIA_TOOL_NAMES = frozenset({"delegate_to_service"})


class CompactionReason(StrEnum):
    """Why a compaction event ran. The first that applies is recorded."""

    BUDGET = "budget"
    AGE = "age"
    REFERENCE = "reference"
    IDLE = "idle"
    CONTEXT_LENGTH = "context_length"


VERBATIM = TurnDecision(TurnMode.VERBATIM)


@dataclass(frozen=True, slots=True)
class CompactionCapabilities:
    """Which references a compacted turn may leave for this profile.

    A stub is only useful to a model that can follow it: a tool stub needs
    ``get_message_history``, an image reference a way to look at the image
    again. A turn that would need a reference the profile cannot follow is
    kept verbatim or dropped whole.
    """

    history_tool: bool
    media_tool: bool


@dataclass(frozen=True, slots=True)
class CompactionCandidate:
    """A turn an event decides on, with what each rendering of it costs.

    ``previous`` is how the turn rendered before the event, or ``None`` for a
    turn that was not in the window (an explicit reference coming back).
    ``compacted_size`` is ``None`` when the turn cannot be compacted for this
    profile.
    """

    key: str
    last_activity: datetime
    verbatim_size: int
    compacted_size: int | None
    previous: TurnDecision | None
    explicit: bool = False


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """The outcome of an event: kept turns' decisions; absent turns are dropped."""

    decisions: dict[str, TurnDecision]
    changed: bool


def plan_compaction(
    candidates: Sequence[CompactionCandidate],
    *,
    target_chars: int,
    min_turns: int,
    cutoff: datetime,
    boundary_internal_id: int,
    relevant: Collection[str] = (),
) -> CompactionPlan:
    """Decide every candidate turn; ``candidates`` are oldest first.

    The mandatory set always stays: explicit references, and the newest
    ``min_turns`` turns within the age cap. Turns older than the cap leave
    unless they are explicit references. The rest are reduced until the window
    fits ``target_chars``, least relevant and then oldest first: compacted
    where the profile can follow the stubs, then -- where that is not enough --
    the turns that could not be compacted are dropped whole. Only then are the
    mandatory turns compacted, and only after that are stubs dropped. Mandatory
    turns are never dropped, so an oversized mandatory set is sent anyway.

    Every verbatim turn after the first turn the event changed loses its bound
    thinking up to ``boundary_internal_id``.
    """
    kept = [c for c in candidates if c.explicit or c.last_activity >= cutoff]
    mandatory = {c.key for c in kept if c.explicit} | {
        c.key for c in (kept[-min_turns:] if min_turns > 0 else [])
    }
    modes = {c.key: TurnMode.VERBATIM for c in kept}
    size = sum(c.verbatim_size for c in kept)

    def current_size(candidate: CompactionCandidate) -> int:
        if modes[candidate.key] is TurnMode.COMPACTED:
            assert candidate.compacted_size is not None
            return candidate.compacted_size
        return candidate.verbatim_size

    def compact(candidate: CompactionCandidate) -> None:
        nonlocal size
        if (
            candidate.compacted_size is None
            or modes[candidate.key] is TurnMode.COMPACTED
            or candidate.compacted_size >= candidate.verbatim_size
        ):
            return
        size -= candidate.verbatim_size - candidate.compacted_size
        modes[candidate.key] = TurnMode.COMPACTED

    order = sorted(
        (c for c in kept if c.key not in mandatory),
        key=lambda c: (c.key in relevant, kept.index(c)),
    )

    def drop(candidates: Sequence[CompactionCandidate]) -> None:
        nonlocal size
        for candidate in candidates:
            if size <= target_chars:
                return
            size -= current_size(candidate)
            del modes[candidate.key]

    for candidate in order:
        if size <= target_chars:
            break
        compact(candidate)
    drop([c for c in order if modes[c.key] is TurnMode.VERBATIM])
    # A stub is a few lines, and the way back to the detail, so the mandatory
    # turns are compacted before any stub is dropped.
    for candidate in kept:
        if size <= target_chars:
            break
        if candidate.key in mandatory:
            compact(candidate)
    drop([c for c in order if c.key in modes])

    first_changed: int | None = None
    for index, candidate in enumerate(candidates):
        previous_mode = candidate.previous.mode if candidate.previous else None
        if modes.get(candidate.key) != previous_mode:
            first_changed = index
            break
    decisions: dict[str, TurnDecision] = {}
    for index, candidate in enumerate(candidates):
        mode = modes.get(candidate.key)
        if mode is None:
            continue
        # A turn coming back into the window was generated against a prefix
        # that has changed since, and a strip, once made, survives the turn
        # being compacted, so restoring it verbatim later cannot bring back
        # thinking that was already invalid.
        strip_through = (
            candidate.previous.strip_through
            if candidate.previous is not None
            else boundary_internal_id
        )
        if first_changed is not None and index > first_changed:
            strip_through = boundary_internal_id
        decisions[candidate.key] = TurnDecision(mode=mode, strip_through=strip_through)
    return CompactionPlan(decisions=decisions, changed=first_changed is not None)


def without_bound_thinking(message: LLMMessage) -> LLMMessage:
    """*message* without Anthropic thinking blocks, which bind to their prefix.

    Gemini thought signatures and OpenAI response output are left alone: they
    are not bound to the prefix, and Gemini's must not be trimmed inside a turn.
    """
    if not isinstance(message, AssistantMessage):
        return message
    metadata = message.provider_metadata
    if isinstance(metadata, dict) and metadata.get("provider") == "anthropic":
        return message.model_copy(update={"provider_metadata": None})
    return message


def _one_line(text: str | None) -> str:
    return next(
        (line.strip() for line in (text or "").splitlines() if line.strip()), ""
    )


def _attachment_reference(attachment_id: str | None) -> TextContentPart:
    if attachment_id is None:
        return TextContentPart(type="text", text="[An attachment, not shown]")
    return TextContentPart(
        type="text",
        text=(
            f"[Image attachment {attachment_id}, not shown here; delegating with "
            "its id lets a model look at it again]"
        ),
    )


def _compacted_user_content(
    content: str | list[ContentPart],
) -> str | list[ContentPart]:
    if isinstance(content, str):
        return content
    parts: list[ContentPart] = []
    for part in content:
        if isinstance(part, ImageUrlContentPart | AttachmentContentPart):
            parts.append(_attachment_reference(part.attachment_id))
        else:
            parts.append(part)
    return parts


def has_attachments(rows: Sequence[MessageWithMetadata]) -> bool:
    return any(
        isinstance(row.message, UserMessage)
        and isinstance(row.message.content, list)
        and any(
            isinstance(part, ImageUrlContentPart | AttachmentContentPart)
            for part in row.message.content
        )
        for row in rows
    )


def has_tool_calls(rows: Sequence[MessageWithMetadata]) -> bool:
    return any(
        isinstance(row.message, AssistantMessage) and row.message.tool_calls
        for row in rows
    )


def can_compact(
    rows: Sequence[MessageWithMetadata], capabilities: CompactionCapabilities
) -> bool:
    """Whether compacting the turn leaves only references the profile can follow."""
    if has_tool_calls(rows) and not capabilities.history_tool:
        return False
    return not (has_attachments(rows) and not capabilities.media_tool)


def render_compacted_turn(
    rows: Sequence[MessageWithMetadata], timezone: ZoneInfo
) -> list[LLMMessage]:
    """A turn reduced to what the user said, stubs, and the final answer.

    Each tool is named once in a one-line stub pointing at
    ``get_message_history``; attachments become references by id; errors are
    one line. A tool call that activated on-demand tools keeps its call and a
    stub result carrying the activation, so compacting a turn never changes the
    tool set (``family_assistant.llm.deferred_tools``). No provider replay
    state survives, so every adapter renders the turn from these messages.

    The answer carries the taint every dropped row introduced, so a compacted
    turn taints the window exactly as long, and as much, as the verbatim turn.
    """
    rendered: list[LLMMessage] = []
    tool_counts: Counter[str] = Counter()
    activating: list[tuple[ToolCallItem, ToolMessage]] = []
    calls_by_id: dict[str, ToolCallItem] = {}
    answer: str | None = None
    errors: list[str] = []
    started_at = rows[0].timestamp if rows else None
    for row in rows:
        message = row.message
        if isinstance(message, UserMessage):
            rendered.append(
                with_sent_at(
                    message.model_copy(
                        update={"content": _compacted_user_content(message.content)}
                    ),
                    timezone,
                )
            )
        elif isinstance(message, SystemMessage):
            rendered.append(message)
        elif isinstance(message, AssistantMessage):
            for tool_call in message.tool_calls or ():
                tool_counts[tool_call.function.name] += 1
                calls_by_id[tool_call.id] = tool_call
            if message.content:
                answer = message.content
        elif isinstance(message, ToolMessage):
            call = calls_by_id.get(message.tool_call_id)
            if message.activated_tools and call is not None:
                activating.append((call, message))
        elif isinstance(message, ErrorMessage):
            errors.append(f"I encountered an error: {_one_line(message.content)}")

    for call, result in activating:
        rendered.extend((
            AssistantMessage(
                tool_calls=[
                    ToolCallItem(
                        id=call.id,
                        type=call.type,
                        function=ToolCallFunction(
                            name=call.function.name,
                            arguments=call.function.arguments,
                        ),
                    )
                ],
            ),
            ToolMessage(
                tool_call_id=result.tool_call_id,
                name=result.name,
                content="[Result not shown here; get_message_history retrieves it]",
                activated_tools=result.activated_tools,
            ),
        ))

    when = (
        f" at {started_at.astimezone(timezone).strftime('%Y-%m-%d %H:%M')}"
        if started_at is not None
        else ""
    )
    lines = [
        f"[Called {name}{f' {count} times' if count > 1 else ''}{when}; "
        f"{HISTORY_TOOL_NAME} retrieves the calls and results]"
        for name, count in tool_counts.items()
    ]
    lines.extend(errors)
    if answer:
        lines.append(answer)
    if lines:
        rendered.append(
            AssistantMessage(
                content="\n".join(lines),
                taint_metadata=introduced_taint_metadata([
                    row.message
                    for row in rows
                    if not isinstance(row.message, UserMessage)
                ]),
            )
        )
    return rendered
