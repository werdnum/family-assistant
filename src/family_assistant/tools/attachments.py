"""Attachment manipulation tools for LLM usage."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from family_assistant.tools.taint_helpers import (
    inherit_attachment_taint,
    record_attachment_read,
)
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from family_assistant.scripting.apis.attachments import ScriptAttachment
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)

# A read returns lines, so a line is the unit grep and paging work in; pieces
# this long keep a match readable while bounding what one matching line costs.
LONG_LINE_CHARS = 2_000
DEFAULT_READ_MAX_CHARS = 8_000
MAX_READ_MAX_CHARS = 50_000


# Tool Definitions
ATTACHMENT_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "attach_to_response",
            "description": (
                "Attach files/images to your current response being sent to the user. "
                "The attachments will be sent along with your text response. "
                "This is the ONLY way to attach files to your reply — never type "
                "attachment metadata (e.g. `<attachment_metadata>...` blocks or "
                "individual `id:` entries) into your reply text; the user will see it "
                "as raw text. Only use attachment IDs that are accessible in the current "
                "conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "attachment"},
                        "description": "List of attachment IDs to include with this response",
                    }
                },
                "required": ["attachment_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_attachment",
            "description": (
                "Read text content from an attachment with optional filtering and pagination. "
                "Use this to explore large text files or tool results that were auto-converted to attachments. "
                f"Lines longer than {LONG_LINE_CHARS} characters are split into pieces, so "
                "grep, offset and limit work on a file that is one long line, and a "
                "read is capped at max_chars. For JSON, jq_query extracts exactly the "
                "fields you need. "
                "Stored attachment provenance is tracked internally; using external-source content may make later writes, browsing, worker execution, or outgoing messages require approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "attachment",
                        "description": "The UUID of the text attachment to read.",
                    },
                    "grep": {
                        "type": "string",
                        "description": "Optional substring to filter lines (case-insensitive).",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of lines to skip from the beginning (default 0).",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (default 100).",
                        "default": 100,
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            f"Maximum characters to return (default "
                            f"{DEFAULT_READ_MAX_CHARS}, maximum {MAX_READ_MAX_CHARS}). "
                            "A longer read stops early and says which offset "
                            "to continue from."
                        ),
                        "default": DEFAULT_READ_MAX_CHARS,
                    },
                },
                "required": ["attachment_id"],
            },
        },
    },
]


# Tool Implementations
async def read_text_attachment_tool(
    exec_context: ToolExecutionContext,
    attachment_id: ScriptAttachment | str,
    grep: str | None = None,
    offset: int = 0,
    limit: int = 100,
    max_chars: int = DEFAULT_READ_MAX_CHARS,
) -> ToolResult:
    """
    Read text from an attachment with optional filtering and pagination.

    Args:
        exec_context: The execution context
        attachment_id: UUID of the attachment or ScriptAttachment object
        grep: Optional string to filter lines (case-insensitive)
        offset: Number of lines to skip (default 0)
        limit: Maximum number of lines to return (default 100)

    Returns:
        ToolResult with the text content and metadata
    """
    # Extract ID from ScriptAttachment if needed
    if not isinstance(attachment_id, str):
        # ScriptAttachment object (from script contexts)
        attachment_id_str = attachment_id.get_id()
    else:
        attachment_id_str = attachment_id

    logger.info(
        f"Reading text attachment {attachment_id_str} (grep={grep}, offset={offset}, limit={limit})"
    )

    if not exec_context.attachment_registry:
        return ToolResult(text="Error: Attachment registry not available.")

    try:
        return await _read_text_attachment(
            exec_context,
            attachment_id_str,
            grep=grep,
            offset=offset,
            limit=limit,
            max_chars=max(1, min(max_chars, MAX_READ_MAX_CHARS)),
        )
    except Exception as e:
        logger.exception(f"Error reading attachment {attachment_id_str}: {e}")
        return ToolResult(text=f"Error: Failed to read attachment. {e!s}")


async def _read_text_attachment(
    exec_context: ToolExecutionContext,
    attachment_id: str,
    *,
    grep: str | None,
    offset: int,
    limit: int,
    max_chars: int,
) -> ToolResult:
    registry = exec_context.attachment_registry
    if registry is None:
        return ToolResult(text="Error: Attachment registry not available.")

    attachment_metadata = await registry.get_attachment(
        exec_context.db_context, attachment_id, acting_user_id=exec_context.user_id
    )
    await record_attachment_read(
        exec_context,
        attachment_id=attachment_id,
        attachment_metadata=(
            attachment_metadata.metadata if attachment_metadata is not None else None
        ),
        qualifier=f"text:{attachment_id}",
    )
    inherit_attachment_taint(
        exec_context,
        attachment_metadata=(
            attachment_metadata.metadata if attachment_metadata is not None else None
        ),
        attachment_id=attachment_id,
    )

    content_bytes = await registry.get_attachment_content(
        exec_context.db_context, attachment_id, acting_user_id=exec_context.user_id
    )
    if content_bytes is None:
        return ToolResult(
            text=f"Error: Attachment {attachment_id} not found or has no content."
        )
    try:
        content_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ToolResult(text="Error: Attachment content is not valid UTF-8 text.")

    original_lines = content_text.splitlines()
    total_lines = len(_split_long_lines(original_lines))
    if grep:
        lines = _grep_excerpts(original_lines, grep)
        filtered_count = len(lines)
    else:
        lines = _split_long_lines(original_lines)
        filtered_count = total_lines

    paged_lines = _within_budget(lines[offset : offset + limit], max_chars)
    summary = f"Read {len(paged_lines)} lines"
    if grep:
        summary += f" matching '{grep}'"
    summary += f" (offset={offset}, total available={filtered_count})"
    if filtered_count != total_lines:
        summary += f" [out of {total_lines} total lines in file]"

    notes: list[str] = []
    next_offset = offset + len(paged_lines)
    if next_offset < min(offset + limit, filtered_count):
        notes.append(
            f"Stopped at {max_chars} characters. Continue with offset={next_offset}, "
            "or narrow the read with grep."
        )
    if _looks_like_json(content_text):
        notes.append(
            f"This attachment is JSON: jq_query(attachment_id='{attachment_id}', "
            "jq_program='...') returns just the fields you need."
        )

    result_text = "\n".join(paged_lines)
    footer = "".join(f"\n[{note}]" for note in notes)
    return ToolResult(
        text=f"--- Attachment {attachment_id} ({summary}) ---\n{result_text}{footer}",
        data={
            "attachment_id": attachment_id,
            "total_lines": total_lines,
            "filtered_count": filtered_count,
            "offset": offset,
            "limit": limit,
            "lines": paged_lines,
        },
    )


def _split_long_lines(lines: list[str]) -> list[str]:
    """Cut every line longer than ``LONG_LINE_CHARS`` into pieces of that size.

    A tool result stored as compact JSON is one line, so without this a grep
    returns the whole file and ``limit`` bounds nothing.
    """
    pieces: list[str] = []
    for line in lines:
        if len(line) <= LONG_LINE_CHARS:
            pieces.append(line)
            continue
        pieces.extend(
            line[start : start + LONG_LINE_CHARS]
            for start in range(0, len(line), LONG_LINE_CHARS)
        )
    return pieces


def _grep_excerpts(lines: list[str], grep: str) -> list[str]:
    """The lines containing ``grep``, case-insensitively, each match kept whole.

    A matching line within ``LONG_LINE_CHARS`` is returned as it is. A longer
    one contributes a window of that size centred on each match, skipping a
    match the previous window already shows. Searching the original line rather
    than its pieces is what keeps a match that straddles a piece boundary.
    """
    needle = grep.lower()
    half = max(0, (LONG_LINE_CHARS - len(needle)) // 2)
    excerpts: list[str] = []
    for line in lines:
        haystack = line.lower()
        if needle not in haystack:
            continue
        if len(line) <= LONG_LINE_CHARS:
            excerpts.append(line)
            continue
        shown_until = 0
        position = haystack.find(needle)
        while position != -1:
            if position + len(needle) > shown_until:
                start = max(0, position - half)
                end = min(len(line), start + LONG_LINE_CHARS)
                excerpts.append(line[start:end])
                shown_until = end
            position = haystack.find(needle, position + 1)
    return excerpts


def _within_budget(lines: list[str], max_chars: int) -> list[str]:
    """The leading lines whose total length fits in ``max_chars``, at least one."""
    kept: list[str] = []
    used = 0
    for line in lines:
        if kept and used + len(line) > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    return kept


def _looks_like_json(text: str) -> bool:
    return text.lstrip()[:1] in {"{", "["}


def _response_attachment_id(attachment: ScriptAttachment | str) -> str:
    if isinstance(attachment, str):
        logger.debug(f"Processing string attachment ID: {attachment}")
        return attachment

    attachment_id = attachment.get_id()
    logger.debug(
        f"Processing ScriptAttachment: {attachment_id} - {attachment.get_description()}"
    )
    return attachment_id


async def attach_to_response_tool(
    exec_context: ToolExecutionContext,
    attachment_ids: list[ScriptAttachment | str],
) -> str:
    """
    Attach files/images to the current LLM response.

    This tool signals to the processing service that the specified attachments
    should be sent along with the current response to the user.

    Args:
        exec_context: The execution context
        attachment_ids: List of ScriptAttachment objects or string IDs to attach to the response

    Returns:
        A JSON string indicating the attachments have been queued
    """

    logger.info(
        f"Executing attach_to_response_tool with {len(attachment_ids)} attachment(s)"
    )

    # Validate that we have attachment registry
    if not exec_context.attachment_registry:
        logger.error("AttachmentRegistry not available in execution context")
        return json.dumps({
            "status": "error",
            "message": "AttachmentRegistry not available",
        })

    # Extract attachment IDs from ScriptAttachment objects or use string IDs directly
    validated_ids = []
    for attachment in attachment_ids:
        try:
            validated_ids.append(_response_attachment_id(attachment))
        except Exception as e:
            logger.error(f"Error processing attachment object: {e}")
            continue

    if not validated_ids:
        return json.dumps({"status": "error", "message": "No valid attachments found"})

    logger.info(
        f"Successfully processed {len(validated_ids)} attachment(s) for response"
    )

    # Return JSON with a clear status message that the LLM can understand
    attachment_word = "attachment" if len(validated_ids) == 1 else "attachments"
    return json.dumps({
        "status": "attachments_queued",
        "attachment_ids": validated_ids,
        "count": len(validated_ids),
        "message": f"Successfully attached {len(validated_ids)} {attachment_word} to this response. The {attachment_word} will be sent with this message. No further action needed.",
    })
