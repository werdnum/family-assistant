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

import asyncio
import base64
import contextlib
import logging
import mimetypes
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio

from family_assistant.scripting.apis.attachments import ScriptAttachment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from family_assistant.tools.types import ToolDefinition, ToolPropertySchema

logger = logging.getLogger(__name__)

type MCPAttachmentMode = Literal["data_uri", "file_path"]

MCP_ATTACHMENT_MODES: frozenset[str] = frozenset({"data_uri", "file_path"})

# `file_path` hands the server a path in our own filesystem, which only means
# anything to a server we spawned ourselves.
FILE_PATH_TRANSPORTS: frozenset[str] = frozenset({"stdio"})

# JSON Schema keywords that wrap a parameter's real shape in a union. FastMCP
# emits one of these for an optional parameter (`list[str] | None` becomes an
# `anyOf` of the array and null branches, with no outer `type`), so the shape we
# need to read is one level down.
_UNION_KEYWORDS = ("anyOf", "oneOf")


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


def _declares_an_array(
    # ast-grep-ignore: no-dict-any - JSON Schema is untyped by nature
    schema: Mapping[str, Any],
) -> bool:
    """Whether ``schema`` describes a list, seeing through an optional's union.

    Only the shape matters here: everything else the server said about the
    parameter is about the string it wanted, which is not what it is being
    given any more. An optional parameter can spell its union three ways --
    ``anyOf``/``oneOf`` branches, or the list form of ``type`` -- and the array
    is the meaningful branch in all of them.
    """
    declared_type = schema.get("type")
    if declared_type == "array":
        return True
    if isinstance(declared_type, list) and "array" in declared_type:
        return True
    for keyword in _UNION_KEYWORDS:
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        return any(
            isinstance(branch, Mapping) and _declares_an_array(branch)
            for branch in branches
        )
    return False


def overlay_attachment_parameters(
    definition: ToolDefinition,
    parameters: Mapping[str, MCPAttachmentMode],
    *,
    server_id: str,
) -> None:
    """Declare configured parameters of ``definition`` attachment-typed, in place.

    The parameter's schema is **replaced**, not decorated: the operator has said
    this parameter takes an attachment, which makes everything the server
    declared about the string it used to want (a ``format: uri``, a pattern, an
    optional's union) describe a value the model is no longer being asked for.
    Leaving any of it in place would contradict the UUID the model must supply —
    a ``type: string`` beside an ``anyOf`` of array and null cannot be satisfied
    at all. Only the description survives, and only the shape (one attachment or
    a list of them) is read off the original.

    Optionality is unaffected: it lives in the schema's ``required`` list, which
    this does not touch.
    """
    tool_name = definition.get("function", {}).get("name", "<unnamed>")
    parameters_schema = definition.get("function", {}).get("parameters", {})
    properties = parameters_schema.get("properties", {})

    for parameter_name, mode in parameters.items():
        parameter_schema = properties.get(parameter_name)
        if not isinstance(parameter_schema, Mapping):
            logger.warning(
                "MCP server %r configures attachment parameter %r on tool %r, "
                "but the server's schema has no such parameter. Ignoring it.",
                server_id,
                parameter_name,
                tool_name,
            )
            continue

        replacement: ToolPropertySchema = (
            {"type": "array", "items": {"type": "attachment"}}
            if _declares_an_array(parameter_schema)
            else {"type": "attachment"}
        )
        description = parameter_schema.get("description")
        if description:
            replacement["description"] = description
        properties[parameter_name] = replacement

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


def _encode_data_uri(mime_type: str, content: bytes) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


class _TempDirectory:
    """A temp directory created on first use and removed when the call ends.

    ``data_uri`` never writes a file, so it never creates one: making (and then
    recursively removing) a directory for every call would put filesystem work
    on the event loop for a mode that needs no filesystem at all.
    """

    def __init__(self) -> None:
        self._path: str | None = None

    async def path(self) -> anyio.Path:
        if self._path is None:
            self._path = await asyncio.to_thread(
                tempfile.mkdtemp, prefix="fa-mcp-attachment-"
            )
        return anyio.Path(self._path)

    async def cleanup(self) -> None:
        if self._path is not None:
            await asyncio.to_thread(shutil.rmtree, self._path, True)
            self._path = None


async def _materialise(
    attachment: ScriptAttachment,
    mode: MCPAttachmentMode,
    directory: _TempDirectory,
) -> str:
    content = await attachment.get_content_async()
    if mode == "data_uri":
        # A document can run to the registry's 100MB limit, and an array
        # repeats the work, so the encode does not belong on the event loop.
        return await asyncio.to_thread(
            _encode_data_uri, attachment.get_mime_type(), content
        )
    path = await directory.path() / f"{attachment.get_id()}{_suffix_for(attachment)}"
    await path.write_bytes(content)
    return str(path)


def _reject_unresolved(
    value: object, parameter_name: str, *, in_array: bool
) -> ScriptAttachment:
    """Return ``value`` as an attachment, or say why it is not one.

    ``process_attachment_arguments`` resolves the UUIDs it recognizes and leaves
    everything else in an array untouched, so without this a model-supplied URL
    in a list parameter would reach the server as itself — the very string the
    parameter stopped accepting when it was configured as an attachment. The
    scalar path already refuses that; this makes an array refuse it too.
    """
    if isinstance(value, ScriptAttachment):
        return value
    where = (
        f"in array parameter '{parameter_name}'"
        if in_array
        else (f"for parameter '{parameter_name}'")
    )
    msg = (
        f"Parameter '{parameter_name}' takes an attachment, but {value!r} {where} "
        f"is not a valid attachment UUID. Attachment IDs are shown in tool result "
        f"messages as '[Attachment ID: ...]'."
    )
    raise ValueError(msg)


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

    Raises:
        ValueError: If a configured parameter holds anything but an attachment.
    """
    if not parameters:
        yield arguments
        return

    directory = _TempDirectory()
    try:
        # ast-grep-ignore: no-dict-any - MCP tool arguments are untyped per the MCP protocol
        materialised: dict[str, Any] = {}
        for key, value in arguments.items():
            mode = parameters.get(key)
            if mode is None or value is None:
                # A null for an optional attachment is the model declining to
                # pass one, which is the server's business, not ours.
                materialised[key] = value
            elif isinstance(value, list):
                materialised[key] = [
                    await _materialise(
                        _reject_unresolved(item, key, in_array=True), mode, directory
                    )
                    for item in value
                ]
            else:
                materialised[key] = await _materialise(
                    _reject_unresolved(value, key, in_array=False), mode, directory
                )
        yield materialised
    finally:
        await directory.cleanup()
