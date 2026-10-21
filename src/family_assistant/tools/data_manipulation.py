"""Data manipulation tools.

This module provides tools for querying and manipulating data attachments,
particularly JSON data using jq queries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.tools import jq_worker
from family_assistant.tools.taint_helpers import inherit_attachment_taint
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

JQ_TIMEOUT_SECONDS = 30.0
JQ_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_JQ_MAX_STDERR_BYTES = 16 * 1024
_JQ_WORKER_PATH = Path(__file__).with_name("jq_worker.py")
_READ_CHUNK_BYTES = 64 * 1024

# Tool Definitions
DATA_MANIPULATION_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "jq_query",
            "description": "Query JSON data from an attachment using jq syntax. Use this to explore and extract specific fields from large JSON datasets without loading the entire content into context. Returns the query result as formatted JSON. Queries are stopped after 30 seconds, and results over 8 MB are rejected, so narrow the query rather than returning whole large datasets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "attachment",
                        "description": "The UUID of the attachment containing JSON data to query.",
                    },
                    "jq_program": {
                        "type": "string",
                        "description": "The jq program/query to run on the JSON data. Examples: 'length' (count items), '.[0]' (first item), 'map(.field)' (extract field from all items), '[.[0].date, .[-1].date]' (get date range).",
                    },
                },
                "required": ["attachment_id", "jq_program"],
            },
        },
    },
]


async def jq_query_tool(
    exec_context: ToolExecutionContext,
    attachment_id: ScriptAttachment | str,
    jq_program: str,
) -> ToolResult:
    """
    Query JSON data from an attachment using jq.

    Args:
        exec_context: The execution context
        attachment_id: UUID of the attachment or ScriptAttachment object
        jq_program: The jq query program to execute

    Returns:
        ToolResult with the query result as formatted JSON
    """
    # Extract ID from ScriptAttachment if needed
    if isinstance(attachment_id, ScriptAttachment):
        attachment_id_str = attachment_id.get_id()
    else:
        attachment_id_str = attachment_id

    logger.info(f"Executing jq query on attachment {attachment_id_str}: {jq_program}")

    db_context = exec_context.db_context

    attachment_registry = exec_context.attachment_registry
    if not attachment_registry:
        logger.error("AttachmentRegistry not available in ToolExecutionContext")
        return ToolResult(text="Error: Attachment registry not available.")

    async def _execute_query() -> ToolResult:
        # Retrieve attachment metadata
        attachment = await attachment_registry.get_attachment(
            db_context, attachment_id_str, acting_user_id=exec_context.user_id
        )

        if not attachment:
            logger.warning(f"Attachment {attachment_id} not found")
            return ToolResult(
                text=f"Error: Attachment with ID {attachment_id} not found."
            )

        inherit_attachment_taint(
            exec_context,
            attachment_metadata=attachment.metadata,
            attachment_id=attachment_id_str,
        )

        # Get attachment content (honoring externally-managed storage_path)
        file_path = attachment_registry.get_attachment_path(
            attachment_id_str,
            stored_path=attachment.storage_path,
            source_type=attachment.source_type,
        )
        if not file_path or not file_path.exists():
            logger.error(f"Attachment file not found for {attachment_id}")
            return ToolResult(
                text=f"Error: Attachment file not found for {attachment_id}."
            )

        return await run_jq_in_subprocess(jq_program, file_path)

    try:
        return await _execute_query()
    except Exception as e:
        logger.exception(f"Error executing jq query on attachment {attachment_id}: {e}")
        return ToolResult(text=f"Error: Failed to execute jq query. {e!s}")


class _OutputTooLargeError(Exception):
    pass


async def _read_capped(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Read a stream to EOF, raising once more than ``limit`` bytes arrive."""
    buffer = bytearray()
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise _OutputTooLargeError
    return bytes(buffer)


async def _read_truncated(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Read a stream to EOF, keeping only the first ``limit`` bytes.

    Draining past the limit keeps a chatty program (jq's ``debug``) from filling
    the pipe and stalling until the timeout.
    """
    buffer = bytearray()
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        buffer.extend(chunk[: max(0, limit - len(buffer))])
    return bytes(buffer)


async def run_jq_in_subprocess(
    jq_program: str,
    json_path: Path,
    *,
    timeout_seconds: float = JQ_TIMEOUT_SECONDS,
    max_output_bytes: int = JQ_MAX_OUTPUT_BYTES,
) -> ToolResult:
    """Run a jq program over a JSON file in a child process.

    The jq binding holds the GIL while a program runs, so a thread does not keep
    the event loop responsive; a child process does, and can be killed when the
    program runs too long or produces too much output.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(_JQ_WORKER_PATH),
        jq_program,
        str(json_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _collect() -> tuple[bytes, bytes, int]:
        assert process.stdout is not None and process.stderr is not None
        stdout, stderr = await asyncio.gather(
            _read_capped(process.stdout, max_output_bytes),
            _read_truncated(process.stderr, _JQ_MAX_STDERR_BYTES),
        )
        return stdout, stderr, await process.wait()

    try:
        stdout, stderr, returncode = await asyncio.wait_for(
            _collect(), timeout=timeout_seconds
        )
    except TimeoutError:
        logger.warning(f"jq program timed out after {timeout_seconds}s: {jq_program}")
        return ToolResult(
            text=f"Error: jq query timed out after {timeout_seconds:g} seconds. "
            "Simplify the query or narrow it to a smaller part of the data."
        )
    except _OutputTooLargeError:
        logger.warning(f"jq output exceeded {max_output_bytes} bytes: {jq_program}")
        return ToolResult(
            text=f"Error: jq query result is larger than {max_output_bytes} bytes. "
            "Narrow the query (select fields, filter, or slice) to return less data."
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    message = stderr.decode("utf-8", errors="replace").strip()
    if returncode == jq_worker.EXIT_JQ_ERROR:
        logger.error(f"jq query error: {message}")
        return ToolResult(text=f"Error: Invalid jq query. {message}")
    if returncode == jq_worker.EXIT_INVALID_JSON:
        return ToolResult(
            text=f"Error: Attachment is not valid JSON. Parse error: {message}"
        )
    if returncode == jq_worker.EXIT_INVALID_UTF8:
        return ToolResult(
            text="Error: Attachment is not valid UTF-8 text. Cannot process as JSON."
        )
    if returncode != 0:
        raise RuntimeError(f"jq worker exited with status {returncode}: {message}")

    text = stdout.decode("utf-8")
    return ToolResult(text=text, data=await asyncio.to_thread(json.loads, text))
