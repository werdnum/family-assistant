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

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.tools.attachment_utils import is_attachment_id

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


def _array_branch(
    # ast-grep-ignore: no-dict-any - JSON Schema is untyped by nature
    schema: Mapping[str, Any],
    # ast-grep-ignore: no-dict-any - JSON Schema is untyped by nature
) -> Mapping[str, Any] | None:
    """The part of ``schema`` that describes a list, or ``None`` if it is scalar.

    An optional parameter can spell its union three ways -- ``anyOf``/``oneOf``
    branches, or the list form of ``type`` -- and the array is the meaningful
    branch in all of them. Returning the branch rather than a yes/no lets the
    caller read the list's own constraints off it, wherever they live.
    """
    declared_type = schema.get("type")
    if declared_type == "array" or (
        isinstance(declared_type, list) and "array" in declared_type
    ):
        return schema
    for keyword in _UNION_KEYWORDS:
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if isinstance(branch, Mapping):
                found = _array_branch(branch)
                if found is not None:
                    return found
    return None


# Constraints on the list itself rather than on the strings it used to hold, so
# they still describe what the server will accept once the items are
# attachments. A multi-image tool that needs two of them still needs two.
_ARRAY_CONSTRAINTS = ("minItems", "maxItems")


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
    at all. What survives is what still describes the new value: the
    description, the shape (one attachment or a list of them), and a list's own
    cardinality constraints, which bound how many attachments the server wants
    and are unaffected by what each one is.

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

        array_branch = _array_branch(parameter_schema)
        replacement: ToolPropertySchema = (
            {"type": "attachment"}
            if array_branch is None
            else {"type": "array", "items": {"type": "attachment"}}
        )
        if array_branch is not None:
            for constraint in _ARRAY_CONSTRAINTS:
                value = array_branch.get(constraint)
                if isinstance(value, int):
                    replacement[constraint] = value
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


def reject_unresolvable_attachment_arguments(
    # ast-grep-ignore: no-dict-any - MCP tool arguments are untyped per the MCP protocol
    arguments: Mapping[str, Any],
    parameters: Mapping[str, MCPAttachmentMode],
) -> None:
    """Refuse anything but an attachment for a configured parameter, before resolving.

    Resolution is not a validator: ``process_attachment_arguments`` expands a
    ``ScriptToolResult``-shaped wrapper into the attachments it names and
    silently drops the entries it cannot use, so a malformed id inside one
    would leave a shortened -- possibly empty -- array and the call would go
    through as if nothing were wrong. Checking the arguments as the caller gave
    them is what makes that visible, and it is the only place the original
    entry still exists to complain about.

    A script resolves its own attachments before dispatch, so an already-resolved
    ``ScriptAttachment`` is accepted here too.

    Raises:
        ValueError: If a configured parameter holds anything else.
    """
    for parameter_name in parameters:
        value = arguments.get(parameter_name)
        if value is None:
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, ScriptAttachment):
                continue
            if isinstance(candidate, str) and is_attachment_id(candidate):
                continue
            msg = (
                f"Parameter '{parameter_name}' takes an attachment, but "
                f"{candidate!r} is not an attachment UUID. Attachment IDs are "
                f"shown in tool result messages as '[Attachment ID: ...]'."
            )
            raise ValueError(msg)


def _write_attachment_file(
    directory: str,
    attachment_id: str,
    filename: str | None,
    mime_type: str,
    content: bytes,
) -> str:
    """Name and write the attachment's file. Runs in a worker thread.

    Both halves are filesystem work and belong on the same hop off the event
    loop: ``mimetypes.guess_extension`` initialises its database on first use by
    reading the system MIME files, and the write is a write.
    """
    suffix = Path(filename).suffix if filename else ""
    if not suffix:
        suffix = mimetypes.guess_extension(mime_type) or ""
    path = Path(directory) / f"{attachment_id}{suffix}"
    path.write_bytes(content)
    return str(path)


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

    async def path(self) -> str:
        if self._path is None:
            self._path = await asyncio.to_thread(
                tempfile.mkdtemp, prefix="fa-mcp-attachment-"
            )
        return self._path

    async def cleanup(self) -> None:
        """Remove the directory, reporting rather than hiding a failure.

        Logged rather than raised: this runs in a ``finally``, where raising
        would replace the call's own outcome (or its exception) with a failure
        the caller can do nothing about, and would not remove the file either.
        An ERROR reaches the persistent error log the diagnostics endpoints
        surface, which is where a leftover copy of a user's attachment needs to
        show up.
        """
        if self._path is None:
            return
        path, self._path = self._path, None
        try:
            await asyncio.to_thread(shutil.rmtree, path)
        except OSError:
            logger.exception(
                "Failed to remove temporary attachment directory %s. A copy of "
                "the user's attachment may remain on disk.",
                path,
            )


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
    return await asyncio.to_thread(
        _write_attachment_file,
        await directory.path(),
        attachment.get_id(),
        attachment.get_filename(),
        attachment.get_mime_type(),
        content,
    )


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
