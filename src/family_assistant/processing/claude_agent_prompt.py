"""Rendering a turn's prepared messages into a Claude Agent SDK session's input.

A session starts empty, so the conversation the processing service prepared --
system prompt, the history window, the message that opened this turn -- has to
reach it through the two inputs a session takes: a system prompt and the first
user message. Earlier turns are rendered as a transcript inside that message;
the history Family Assistant saves stays the only copy of the conversation.
See docs/design/claude-agent-sdk-profile.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.llm.messages import (
    AssistantMessage,
    AttachmentContentPart,
    FileContentPart,
    ImageUrlContentPart,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm.messages import ContentPart, LLMMessage

_DATA_URL = re.compile(r"^data:(?P<media_type>[^;,]+);base64,(?P<data>.*)$", re.DOTALL)

HISTORY_PREAMBLE = (
    "The conversation so far, oldest first, as a transcript. It is context for "
    "the request that follows it, not a request in itself."
)

# The content of an Anthropic user message, in the shape the Agent SDK's
# streaming input passes through to Claude Code unchanged.
type SessionContentBlock = dict[str, object]


@dataclass(frozen=True)
class SessionPrompt:
    """What a session is started with."""

    system_prompt: str
    content: list[SessionContentBlock]


def build_session_prompt(
    messages: Sequence[LLMMessage],
    *,
    system_prompt_additions: Sequence[str | None] = (),
) -> SessionPrompt:
    """Split *messages* into a session's system prompt and opening message.

    The opening message is the trailing run of user messages -- the request
    that opened the turn, and any scaffolding the service appended after it.
    Everything between the system prompt and that run is earlier conversation
    and is rendered as a transcript ahead of it.
    """
    system_parts: list[str] = []
    body = list(messages)
    while body and isinstance(first := body[0], SystemMessage):
        system_parts.append(first.content)
        body.pop(0)
    system_parts.extend(addition for addition in system_prompt_additions if addition)

    split = len(body)
    while split > 0 and isinstance(body[split - 1], UserMessage):
        split -= 1
    history, opening = body[:split], body[split:]
    if not opening:
        msg = "A Claude Agent SDK turn needs a user message to open it."
        raise ValueError(msg)

    content: list[SessionContentBlock] = []
    if history:
        content.append({"type": "text", "text": render_transcript(history)})
    for message in opening:
        assert isinstance(message, UserMessage)
        content.extend(_user_content_blocks(message))
    return SessionPrompt(system_prompt="\n\n".join(system_parts), content=content)


def render_transcript(history: Sequence[LLMMessage]) -> str:
    """Earlier conversation as one deterministic text block."""
    lines = ["<conversation_history>", HISTORY_PREAMBLE, ""]
    for message in history:
        lines.extend(_transcript_lines(message))
    lines.append("</conversation_history>")
    return "\n".join(lines)


def _transcript_lines(message: LLMMessage) -> list[str]:
    if isinstance(message, UserMessage):
        return [f"[user] {_user_text(message)}"]
    if isinstance(message, AssistantMessage):
        lines: list[str] = []
        if message.content:
            lines.append(f"[assistant] {message.content}")
        for call in message.tool_calls or ():
            arguments = call.function.arguments
            rendered = (
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments, sort_keys=True, ensure_ascii=False)
            )
            lines.append(f"[assistant called tool {call.function.name}] {rendered}")
        return lines
    if isinstance(message, ToolMessage):
        return [f"[tool {message.name} result] {message.content}"]
    if isinstance(message, SystemMessage):
        return [f"[system] {message.content}"]
    return [f"[{message.role}] {getattr(message, 'content', '')}"]


def _user_text(message: UserMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return " ".join(_part_as_text(part) for part in message.content)


def _part_as_text(part: ContentPart) -> str:
    if isinstance(part, TextContentPart):
        return part.text
    if isinstance(part, ImageUrlContentPart):
        if part.attachment_id:
            return f"[image attachment {part.attachment_id}]"
        return "[image]"
    if isinstance(part, AttachmentContentPart):
        return f"[attachment {part.attachment_id}]"
    if isinstance(part, FileContentPart):
        return "[file]"
    msg = f"Unknown content part {type(part).__name__}"
    raise TypeError(msg)


def _user_content_blocks(message: UserMessage) -> list[SessionContentBlock]:
    """The opening message's own content, with images passed through."""
    if isinstance(message.content, str):
        return [{"type": "text", "text": message.content}]
    blocks: list[SessionContentBlock] = []
    for part in message.content:
        if isinstance(part, ImageUrlContentPart):
            blocks.append(_image_block(part))
        else:
            blocks.append({"type": "text", "text": _part_as_text(part)})
    return blocks


def _image_block(part: ImageUrlContentPart) -> SessionContentBlock:
    url = part.image_url["url"]
    match = _DATA_URL.match(url)
    if match is not None:
        source: dict[str, str] = {
            "type": "base64",
            "media_type": match["media_type"],
            "data": match["data"],
        }
    else:
        source = {"type": "url", "url": url}
    return {"type": "image", "source": source}
