"""Attachment plumbing for MCP tools.

MCP servers speak their own schemas: an image parameter is a `string` holding a
data URI or a filesystem path, never one of our attachment UUIDs. This module
bridges the two, driven by a per-server ``attachment_parameters`` block that
names which parameters of which tools carry an attachment and in what form the
server wants it.

Two hooks, matching the two points where every other provider handles
attachments:

- :func:`overlay_attachment_parameters` runs at definition time, marking the
  named fields ``type: attachment`` so the existing translation advertises them
  to the model as attachment UUIDs.
- :func:`materialised_attachment_arguments` runs at execution time, after
  ``process_attachment_arguments`` has resolved those UUIDs, and renders each
  attachment into the form the server asked for.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import mimetypes
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio

from family_assistant.scripting.apis.attachments import ScriptAttachment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from family_assistant.tools.types import ToolDefinition

logger = logging.getLogger(__name__)

type MCPAttachmentMode = Literal["data_uri", "file_path"]

MCP_ATTACHMENT_MODES: frozenset[str] = frozenset({"data_uri", "file_path"})

# `file_path` hands the server a path in our own filesystem, which only means
# anything to a server we spawned ourselves.
FILE_PATH_TRANSPORTS: frozenset[str] = frozenset({"stdio"})

# Schema keywords that describe the *server's* string format (a URI, a path
# pattern). Once a parameter is declared an attachment they contradict what the
# model is being asked for, so the overlay drops them.
_CONFLICTING_SCHEMA_KEYS = ("format", "pattern", "enum", "contentMediaType")


def normalize_attachment_parameters(
    # ast-grep-ignore: no-dict-any - Raw MCP server config is untyped JSON
    attachment_parameters: Mapping[str, Any] | None,
) -> dict[str, dict[str, MCPAttachmentMode]]:
    """Validate a server's ``attachment_parameters`` block.

    Raises:
        ValueError: If the block is malformed or names an unknown mode.
    """
    if not attachment_parameters:
        return {}

    normalized: dict[str, dict[str, MCPAttachmentMode]] = {}
    for tool_name, raw_parameters in attachment_parameters.items():
        if not isinstance(raw_parameters, Mapping):
            msg = (
                f"attachment_parameters for tool {tool_name!r} must map parameter "
                f"names to modes, got {type(raw_parameters).__name__}"
            )
            raise TypeError(msg)
        parameters: dict[str, MCPAttachmentMode] = {}
        for parameter_name, raw_mode in raw_parameters.items():
            if raw_mode not in MCP_ATTACHMENT_MODES:
                msg = (
                    f"Unknown attachment mode {raw_mode!r} for "
                    f"{tool_name}.{parameter_name}. Expected one of: "
                    f"{', '.join(sorted(MCP_ATTACHMENT_MODES))}."
                )
                raise ValueError(msg)
            parameters[parameter_name] = raw_mode
        normalized[tool_name] = parameters
    return normalized


def file_path_mode_is_supported(transport: str) -> bool:
    """Whether ``file_path`` materialisation makes sense for a transport."""
    return transport.lower() in FILE_PATH_TRANSPORTS


def overlay_attachment_parameters(
    definition: ToolDefinition,
    parameters: Mapping[str, MCPAttachmentMode],
    *,
    server_id: str,
) -> None:
    """Mark configured parameters of ``definition`` as attachment-typed, in place.

    The overlay follows the schema's own shape: an array parameter gets its
    ``items`` marked, so a list of attachment UUIDs is what the model is asked
    for, and everything else is marked directly.
    """
    tool_name = definition.get("function", {}).get("name", "<unnamed>")
    properties = (
        definition.get("function", {}).get("parameters", {}).get("properties", {})
    )

    for parameter_name, mode in parameters.items():
        parameter_schema = properties.get(parameter_name)
        if not isinstance(parameter_schema, dict):
            logger.warning(
                "MCP server %r configures attachment parameter %r on tool %r, "
                "but the server's schema has no such parameter. Ignoring it.",
                server_id,
                parameter_name,
                tool_name,
            )
            continue

        target = parameter_schema
        if parameter_schema.get("type") == "array":
            items = parameter_schema.setdefault("items", {})
            if not isinstance(items, dict):
                logger.warning(
                    "MCP server %r configures attachment parameter %r on tool %r, "
                    "but its 'items' schema is not an object. Ignoring it.",
                    server_id,
                    parameter_name,
                    tool_name,
                )
                continue
            target = items

        target["type"] = "attachment"
        for key in _CONFLICTING_SCHEMA_KEYS:
            target.pop(key, None)
        logger.debug(
            "Marked %s.%s as an attachment parameter (%s) for MCP server %r",
            tool_name,
            parameter_name,
            mode,
            server_id,
        )


def _suffix_for(attachment: ScriptAttachment) -> str:
    filename = attachment.get_filename()
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix
    return mimetypes.guess_extension(attachment.get_mime_type()) or ""


def _to_data_uri(attachment: ScriptAttachment, content: bytes) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{attachment.get_mime_type()};base64,{encoded}"


async def _to_file_path(
    attachment: ScriptAttachment, content: bytes, directory: anyio.Path
) -> str:
    path = directory / f"{attachment.get_id()}{_suffix_for(attachment)}"
    await path.write_bytes(content)
    return str(path)


async def _materialise(
    attachment: ScriptAttachment,
    mode: MCPAttachmentMode,
    directory: anyio.Path,
) -> str:
    content = await attachment.get_content_async()
    if mode == "data_uri":
        return _to_data_uri(attachment, content)
    return await _to_file_path(attachment, content, directory)


@contextlib.asynccontextmanager
async def materialised_attachment_arguments(
    # ast-grep-ignore: no-dict-any - MCP tool arguments are untyped per the MCP protocol
    arguments: dict[str, Any],
    parameters: Mapping[str, MCPAttachmentMode],
    # ast-grep-ignore: no-dict-any - MCP tool arguments are untyped per the MCP protocol
) -> AsyncIterator[dict[str, Any]]:
    """Yield ``arguments`` with resolved attachments rendered for the server.

    Any temporary file written for ``file_path`` mode lives only for the
    duration of the block: MCP calls are synchronous request/response, so the
    server has read the file by the time the call returns, and leaving a copy of
    the user's content on disk afterwards is not something the caller asked for.
    """
    if not parameters:
        yield arguments
        return

    with tempfile.TemporaryDirectory(prefix="fa-mcp-attachment-") as temp_dir:
        directory = anyio.Path(temp_dir)
        # ast-grep-ignore: no-dict-any - MCP tool arguments are untyped per the MCP protocol
        materialised: dict[str, Any] = {}
        for key, value in arguments.items():
            mode = parameters.get(key)
            if mode is None:
                materialised[key] = value
            elif isinstance(value, ScriptAttachment):
                materialised[key] = await _materialise(value, mode, directory)
            elif isinstance(value, list):
                materialised[key] = [
                    await _materialise(item, mode, directory)
                    if isinstance(item, ScriptAttachment)
                    else item
                    for item in value
                ]
            else:
                materialised[key] = value
        yield materialised
