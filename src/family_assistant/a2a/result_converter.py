"""Convert A2A Task results to FA ChatInteractionResult."""

# ruff: noqa: PLC0415 — ChatInteractionResult needed at runtime for .error()/.success()
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import a2a.types as a2a_types

from family_assistant.a2a.types import Message, Part, Role, Task, TaskState

if TYPE_CHECKING:
    from family_assistant.processing.types import ChatInteractionResult

logger = logging.getLogger(__name__)


def _extract_status_text(message: Message | None, fallback: str) -> str:
    """Extract text from a TaskStatus message, or return fallback."""
    if message is None:
        return fallback
    texts: list[str] = []
    for part in message.parts:
        inner = part.root
        if isinstance(inner, a2a_types.TextPart):
            texts.append(inner.text)
    return " ".join(texts) if texts else fallback


def a2a_task_to_chat_result(task: Task) -> ChatInteractionResult:
    """Convert a completed A2A Task to a ChatInteractionResult.

    Extracts text and files from artifacts first, falling back to the
    terminal agent message if no artifacts are present.
    """
    from family_assistant.processing.types import ChatInteractionResult as CIR

    if task.status.state == TaskState.failed:
        error_msg = _extract_status_text(
            task.status.message, "Remote agent task failed"
        )
        return CIR.error(
            text_reply=f"Remote agent error: {error_msg}",
            error_traceback=f"A2A task failed: {error_msg}",
        )

    if task.status.state == TaskState.canceled:
        return CIR.error(
            text_reply="Remote agent task was cancelled.",
            error_traceback="A2A task was cancelled",
        )

    if task.status.state in {
        TaskState.auth_required,
        TaskState.rejected,
    }:
        msg = _extract_status_text(task.status.message, str(task.status.state))
        return CIR.error(
            text_reply=f"Remote agent declined: {msg}",
            error_traceback=f"A2A task state: {task.status.state}, message: {msg}",
        )

    if task.status.state == TaskState.input_required:
        msg = _extract_status_text(
            task.status.message, "Remote agent needs more information"
        )
        return CIR.error(
            text_reply=f"Remote agent needs more information: {msg}",
            error_traceback=f"A2A task state: input_required, message: {msg}",
        )

    if task.status.state != TaskState.completed:
        return CIR.error(
            text_reply=f"Remote agent returned unexpected state: {task.status.state}",
            error_traceback=f"A2A task in non-terminal state: {task.status.state}",
        )

    text_parts: list[str] = []

    _extract_text_from_parts(task.artifacts or [], text_parts)

    # Fall back to the terminal agent message if no artifacts produced text
    if not text_parts and task.history:
        for msg in reversed(task.history):
            if msg.role == Role.agent:
                _extract_text_from_message_parts(msg.parts, text_parts)
                break

    if not text_parts:
        return CIR.error(
            text_reply="Remote agent completed but produced no output.",
            error_traceback="A2A task completed with no text, data, or file parts",
        )

    return CIR.success(text_reply="\n\n".join(text_parts))


def _extract_text_from_parts(
    artifacts: list[a2a_types.Artifact], text_parts: list[str]
) -> None:
    for artifact in artifacts:
        _extract_text_from_message_parts(artifact.parts, text_parts)


def _extract_text_from_message_parts(parts: list[Part], text_parts: list[str]) -> None:
    for part in parts:
        inner = part.root
        if isinstance(inner, a2a_types.TextPart):
            text_parts.append(inner.text)
        elif isinstance(inner, a2a_types.DataPart):
            text_parts.append(json.dumps(inner.data, indent=2))
        elif isinstance(inner, a2a_types.FilePart):
            file = inner.file
            if isinstance(file, a2a_types.FileWithUri):
                text_parts.append(f"[File: {file.uri}]")
            else:
                mime = getattr(file, "mime_type", "unknown") or "unknown"
                text_parts.append(f"[Inline file: {mime}]")
