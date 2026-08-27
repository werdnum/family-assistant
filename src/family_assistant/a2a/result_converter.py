"""Convert A2A Task results to FA ChatInteractionResult."""

# ruff: noqa: PLC0415 — ChatInteractionResult needed at runtime for .error()/.success()
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import a2a.types as a2a_types

from family_assistant.a2a.attachments import default_a2a_peer_taint_source
from family_assistant.a2a.types import Message, Part, Role, Task, TaskState

if TYPE_CHECKING:
    from family_assistant.a2a.attachments import A2AAttachmentTransfer
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


async def a2a_task_to_chat_result(
    task: Task,
    *,
    attachments: A2AAttachmentTransfer | None = None,
    conversation_id: str | None = None,
    owner_user_id: str | None = None,
) -> ChatInteractionResult:
    """Convert a completed A2A Task to a ChatInteractionResult.

    Extracts text and files from artifacts first, falling back to the
    terminal agent message if no artifacts are present. Files the remote sent
    inline are registered as FA attachments and returned as ``attachment_ids``;
    a file offered only as a remote URI stays a textual reference.
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

    attachment_ids: list[str] = []
    if attachments is not None:
        attachment_ids = await attachments.store_task_files(
            task,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            taint_sources=(
                default_a2a_peer_taint_source(
                    task.id,
                    "File returned by a remote A2A agent; the agent's own inputs "
                    "are not visible here, so its output carries peer trust.",
                ),
            ),
        )

    text_parts: list[str] = []
    # Inline files became real attachments above; describing them again as text
    # would duplicate them in the reply.
    stored_inline = attachments is not None
    for artifact in task.artifacts or []:
        _extract_text_from_message_parts(artifact.parts, text_parts, stored_inline)

    # Fall back to the terminal agent message if no artifacts produced text
    if not text_parts and task.history:
        for msg in reversed(task.history):
            if msg.role == Role.agent:
                _extract_text_from_message_parts(msg.parts, text_parts, stored_inline)
                break

    if not text_parts and not attachment_ids:
        return CIR.error(
            text_reply="Remote agent completed but produced no output.",
            error_traceback="A2A task completed with no text, data, or file parts",
        )

    return CIR.success(
        text_reply="\n\n".join(text_parts),
        attachment_ids=attachment_ids or None,
    )


def _extract_text_from_message_parts(
    parts: list[Part], text_parts: list[str], stored_inline: bool
) -> None:
    for part in parts:
        inner = part.root
        if isinstance(inner, a2a_types.TextPart):
            text_parts.append(inner.text)
        elif isinstance(inner, a2a_types.DataPart):
            text_parts.append(json.dumps(inner.data, indent=2))
        elif isinstance(inner, a2a_types.FilePart):
            text_parts.extend(_describe_file_part(inner, stored_inline))


def _describe_file_part(
    file_part: a2a_types.FilePart, stored_inline: bool
) -> list[str]:
    """Textual stand-in for a file part that did not become an attachment."""
    file = file_part.file
    if isinstance(file, a2a_types.FileWithUri) and not file.uri.startswith("data:"):
        return [f"[File: {file.uri}]"]
    if stored_inline:
        return []
    mime = getattr(file, "mime_type", "unknown") or "unknown"
    return [f"[Inline file: {mime}]"]
