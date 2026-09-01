"""Serialize a deployment's tool registry so the eval can resolve its tools.

Every entry point in this package takes a ``descriptor_registry`` and defaults
it to the *local* tool list, which is the static set compiled into the source
tree. A deployment's real registry is larger: MCP servers are discovered at
startup from configuration, so their tools exist only in a running process.
Cases and templates naming those tools cannot be validated, extracted or
replayed against the local list -- ``resolve_tool_descriptor`` raises, and the
case drops out of the harness.

A snapshot is that running registry written down: one JSON file mapping tool
name to descriptor, produced by ``scripts/dump_tool_registry.py`` against a
configured deployment and consumed by both the history extractor and the eval
runner. One artifact, two consumers, so a template extracted under a registry
can be replayed under the same one -- including on a machine that has no MCP
servers at all, which is where the eval otherwise cannot run.

Loading fails loudly. A snapshot with an unknown version, an unrecognized tag
or a malformed descriptor raises rather than resolving what it can: a registry
that silently loses entries turns into cases skipped for "unknown tool", which
reads as a smaller corpus rather than as a broken input file.

The snapshot is deployment data, not source. MCP parameter schemas can enumerate
a household's own vocabulary -- entity ids, room names, calendar names -- so a
snapshot taken from a real deployment belongs under the private eval tree with
the templates, not in the repository.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

from family_assistant.tools.metadata import ToolDescriptor, ToolTag

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from family_assistant.tools.types import ToolDefinition

SNAPSHOT_VERSION = 1

_ORIGINS = frozenset({"local", "mcp"})

# Every serialized descriptor carries every one of these. The writer builds
# entries from this tuple and the reader requires all of them, so a field added
# to ``ToolDescriptor`` cannot reach a snapshot on one side only -- and, more to
# the point, no reader gets to decide per field whether absent means empty.
# Several of these have an empty value that is also their default, which is
# exactly when a missing key loads quietly and changes the reviewer input:
# ``destination_argument_paths`` drops the trusted-destination echo, and
# ``mcp_server_id`` renders a null server into the reviewer's tool context.
DESCRIPTOR_FIELDS = (
    "definition",
    "tags",
    "origin",
    "mcp_server_id",
    "summary",
    "destination_argument_paths",
    "deferred_confirmation_eligible",
)


class RegistrySnapshotError(Exception):
    """A registry snapshot could not be read as one."""


def descriptors_to_snapshot(
    descriptors: Iterable[ToolDescriptor],
) -> dict[str, object]:
    """Render descriptors as the JSON-ready snapshot envelope.

    Duplicate names raise. The application refuses to start with two tools of
    one name because providers reject duplicate function declarations, so a
    snapshot carrying both would describe a registry no deployment can serve,
    and silently keeping the last would pick between them at random.
    """
    tools: dict[str, object] = {}
    for descriptor in descriptors:
        if descriptor.name in tools:
            raise RegistrySnapshotError(
                f"Duplicate tool name {descriptor.name!r} in the registry being "
                "snapshotted; a deployment cannot advertise two tools of one name."
            )
        serialized: dict[str, object] = {
            "definition": descriptor.definition,
            "tags": sorted(tag.value for tag in descriptor.tags),
            "origin": descriptor.origin,
            "mcp_server_id": descriptor.mcp_server_id,
            "summary": descriptor.summary,
            "destination_argument_paths": list(descriptor.destination_argument_paths),
            "deferred_confirmation_eligible": (
                descriptor.deferred_confirmation_eligible
            ),
        }
        if set(serialized) != set(DESCRIPTOR_FIELDS):
            raise RegistrySnapshotError(
                f"Serializer writes {sorted(serialized)} but DESCRIPTOR_FIELDS "
                f"declares {sorted(DESCRIPTOR_FIELDS)}; the reader requires the "
                "declared set, so the two must agree."
            )
        tools[descriptor.name] = serialized
    return {"snapshot_version": SNAPSHOT_VERSION, "tools": tools}


def snapshot_to_registry(payload: object) -> dict[str, ToolDescriptor]:
    """Rebuild a name->descriptor registry from a parsed snapshot envelope."""
    if not isinstance(payload, dict):
        raise RegistrySnapshotError("Snapshot is not a JSON object.")
    version = payload.get("snapshot_version")
    if version != SNAPSHOT_VERSION:
        raise RegistrySnapshotError(
            f"Snapshot version {version!r} is not the supported version "
            f"{SNAPSHOT_VERSION}; regenerate it with scripts/dump_tool_registry.py."
        )
    tools = payload.get("tools")
    if not isinstance(tools, dict):
        raise RegistrySnapshotError("Snapshot 'tools' is not a JSON object.")
    return {
        name: _descriptor_from_entry(name, entry)
        for name, entry in cast("dict[str, object]", tools).items()
    }


def registry_digest(registry: Mapping[str, ToolDescriptor] | None) -> str | None:
    """Short digest identifying which tool definitions a run resolved against.

    ``None`` is preserved rather than hashed, matching the rule the report's
    other configuration digests follow: "not supplied" and "supplied" are
    different facts about a run, and not supplying one means the build's own
    local list, which the source revision already identifies.
    """
    if registry is None:
        return None
    serialized = json.dumps(
        descriptors_to_snapshot(registry[name] for name in sorted(registry)),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def load_registry_snapshot(path: Path) -> dict[str, ToolDescriptor]:
    """Read a snapshot file and return the registry it describes."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistrySnapshotError(f"Cannot read snapshot {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistrySnapshotError(
            f"Snapshot {path} is not valid JSON: {exc}"
        ) from exc
    return snapshot_to_registry(payload)


def _descriptor_from_entry(name: str, entry: object) -> ToolDescriptor:
    if not isinstance(entry, dict):
        raise RegistrySnapshotError(f"Snapshot entry for {name!r} is not an object.")
    fields = cast("dict[str, object]", entry)
    absent = [key for key in DESCRIPTOR_FIELDS if key not in fields]
    if absent:
        raise RegistrySnapshotError(
            f"Snapshot entry for {name!r} is missing {', '.join(absent)}; it was "
            "not written by scripts/dump_tool_registry.py."
        )

    definition = fields["definition"]
    _require_usable_definition(name, definition)

    origin = fields["origin"]
    if origin not in _ORIGINS:
        raise RegistrySnapshotError(
            f"Snapshot entry for {name!r} has origin {origin!r}, "
            f"not one of {sorted(_ORIGINS)}."
        )

    return ToolDescriptor(
        name=name,
        definition=cast("ToolDefinition", cast("dict[str, Any]", definition)),
        tags=_tags_from_entry(name, fields["tags"]),
        origin=cast("Any", origin),
        mcp_server_id=_server_id_from_entry(name, origin, fields["mcp_server_id"]),
        summary=_optional_str(name, "summary", fields["summary"]),
        destination_argument_paths=_paths_from_entry(
            name, fields["destination_argument_paths"]
        ),
        deferred_confirmation_eligible=_bool_from_entry(
            name, fields["deferred_confirmation_eligible"]
        ),
    )


def _tags_from_entry(name: str, raw: object) -> frozenset[ToolTag]:
    if not isinstance(raw, list):
        raise RegistrySnapshotError(f"Snapshot entry for {name!r} has no tag list.")
    tags: set[ToolTag] = set()
    for value in cast("list[object]", raw):
        try:
            tags.add(ToolTag(value))
        except ValueError as exc:
            # A tag this build does not know means the snapshot was taken from a
            # different version of the source tree. Policy decisions read tags,
            # so dropping the unknown one would evaluate the tool under a
            # weaker classification than the deployment gave it.
            raise RegistrySnapshotError(
                f"Snapshot entry for {name!r} carries unknown tag {value!r}; "
                "it was taken from a different build than this one."
            ) from exc
    return frozenset(tags)


def _require_usable_definition(name: str, definition: object) -> None:
    """Reject a definition that is shaped like one but cannot serve as one.

    An empty or partial object passes an ``isinstance`` check and then resolves,
    which is worse than not resolving: the extractor derives no argument shapes
    from the absent schema and emits a well-formed, argument-less template
    describing a task shape nothing recorded. The name is checked against the
    key for the same reason -- the reviewer is told the tool's name from the
    definition, so an entry filed under one name and describing another reviews
    the wrong tool while looking resolved.
    """
    if not isinstance(definition, dict):
        raise RegistrySnapshotError(
            f"Snapshot entry for {name!r} has no tool definition object."
        )
    function = cast("dict[str, object]", definition).get("function")
    if not isinstance(function, dict):
        raise RegistrySnapshotError(
            f"Snapshot entry for {name!r} has a definition with no function."
        )
    declared = cast("dict[str, object]", function)
    if declared.get("name") != name:
        raise RegistrySnapshotError(
            f"Snapshot entry for {name!r} describes a tool named "
            f"{declared.get('name')!r}; the key and the definition must agree."
        )
    if not isinstance(declared.get("parameters"), dict):
        raise RegistrySnapshotError(
            f"Snapshot entry for {name!r} declares no parameter schema; a tool "
            "with no schema resolves and then contributes no argument shapes."
        )


def _server_id_from_entry(name: str, origin: object, raw: object) -> str | None:
    """Return the MCP server id, requiring one for an MCP-origin descriptor.

    Present-but-null is meaningful for a local tool and meaningless for an MCP
    one: the reviewer renders the server id into its tool context, so a null
    there describes a tool no server serves.
    """
    server_id = _optional_str(name, "mcp_server_id", raw)
    if origin == "mcp" and server_id is None:
        raise RegistrySnapshotError(
            f"Snapshot entry for {name!r} has origin 'mcp' but no "
            "mcp_server_id; an MCP tool comes from a named server."
        )
    return server_id


def _paths_from_entry(name: str, raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(
        isinstance(item, str) for item in cast("list[object]", raw)
    ):
        raise RegistrySnapshotError(
            f"Snapshot entry for {name!r} has a non-string destination path list."
        )
    return tuple(cast("list[str]", raw))


def _optional_str(name: str, field: str, raw: object) -> str | None:
    if raw is None or isinstance(raw, str):
        return raw
    raise RegistrySnapshotError(
        f"Snapshot entry for {name!r} has a non-string {field}."
    )


def _bool_from_entry(name: str, raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    raise RegistrySnapshotError(
        f"Snapshot entry for {name!r} has a non-boolean deferred_confirmation_eligible."
    )
