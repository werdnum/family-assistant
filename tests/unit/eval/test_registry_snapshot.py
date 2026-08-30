"""Unit tests for the tool-registry snapshot the review eval resolves against."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from family_assistant.eval.tool_call_review.registry_snapshot import (
    SNAPSHOT_VERSION,
    RegistrySnapshotError,
    descriptors_to_snapshot,
    load_registry_snapshot,
    snapshot_to_registry,
)
from family_assistant.eval.tool_call_review.scrub import (
    TaskTemplate,
    TemplatePrivacyError,
    declared_argument_shapes,
)
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.metadata import ToolDescriptor, ToolTag

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.no_db


def _mcp_descriptor(name: str = "plan_trip") -> ToolDescriptor:
    """A stand-in for a tool that exists only on a configured MCP server."""
    return ToolDescriptor(
        name=name,
        definition={
            "type": "function",
            "function": {
                "name": name,
                "description": "Plan a public transport trip.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {"type": "string"},
                        "destination": {"type": "string"},
                    },
                    "required": ["origin", "destination"],
                },
            },
        },
        tags=frozenset({ToolTag.READ_ONLY, ToolTag.OPEN_WORLD}),
        origin="mcp",
        mcp_server_id="transport",
        summary="Plan a trip.",
        destination_argument_paths=("destination",),
    )


def test_the_real_local_registry_round_trips_unchanged() -> None:
    """Every descriptor the source tree ships survives a snapshot exactly.

    Asserted against the live list rather than a fixture: the snapshot has to
    carry whatever fields descriptors actually have, so a field added to
    ``ToolDescriptor`` without a serializer fails here instead of silently
    reaching the eval as a default.
    """
    rebuilt = snapshot_to_registry(descriptors_to_snapshot(LOCAL_TOOL_DESCRIPTORS))

    assert rebuilt == {
        descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS
    }


def test_an_mcp_descriptor_round_trips_through_a_file(tmp_path: Path) -> None:
    descriptor = _mcp_descriptor()
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(descriptors_to_snapshot([descriptor])), encoding="utf-8")

    assert load_registry_snapshot(path) == {"plan_trip": descriptor}


def test_a_snapshot_resolves_a_tool_the_local_registry_cannot() -> None:
    """The point of the snapshot: an MCP tool call becomes extractable.

    Without a registry the same template is refused for an unresolvable tool
    name, which is how a deployment's whole MCP surface dropped out of the
    extracted corpus.
    """
    registry = snapshot_to_registry(descriptors_to_snapshot([_mcp_descriptor()]))
    shapes = declared_argument_shapes(
        "plan_trip",
        {"origin": "Central", "destination": "Bondi"},
        descriptor_registry=registry,
    )
    template = TaskTemplate(
        template_id="tmpl-aaaabbbbcccc",
        boundary="conversation",
        intent_category="<unknown>",
        tool_names=["plan_trip"],
        argument_shapes=shapes,
        sink_class="<unknown>",
        taint_tier="<unclassified>",
        content_kind="none",
    )

    template.validate_committable(descriptor_registry=registry)

    with pytest.raises(TemplatePrivacyError, match="does not resolve in the registry"):
        template.validate_committable()


def test_a_key_outside_the_schema_still_does_not_reach_a_template() -> None:
    """Resolving MCP tools does not widen what may cross the boundary.

    A snapshot makes the tool's schema available, which is exactly what the
    key filter needs; a key the schema does not declare is dropped the same as
    for a local tool.
    """
    registry = snapshot_to_registry(descriptors_to_snapshot([_mcp_descriptor()]))

    shapes = declared_argument_shapes(
        "plan_trip",
        {"origin": "Central", "Alice_gate_code_8391": "1234"},
        descriptor_registry=registry,
    )

    assert shapes == {"origin": "string"}


def test_duplicate_tool_names_are_refused() -> None:
    """A registry no deployment can serve is not written down as if it were."""
    with pytest.raises(RegistrySnapshotError, match="Duplicate tool name"):
        descriptors_to_snapshot([_mcp_descriptor(), _mcp_descriptor()])


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(
            lambda payload: payload.__setitem__("snapshot_version", 99),
            "not the supported version",
            id="future-version",
        ),
        pytest.param(
            lambda payload: payload.__setitem__("tools", []),
            "'tools' is not a JSON object",
            id="tools-not-an-object",
        ),
        pytest.param(
            lambda payload: payload["tools"]["plan_trip"].__setitem__(
                "tags", ["teleportation"]
            ),
            "unknown tag",
            id="unknown-tag",
        ),
        pytest.param(
            lambda payload: payload["tools"]["plan_trip"].__setitem__("origin", "smtp"),
            "has origin",
            id="unknown-origin",
        ),
        pytest.param(
            lambda payload: payload["tools"]["plan_trip"].pop("definition"),
            "no tool definition object",
            id="no-definition",
        ),
    ],
)
def test_a_malformed_snapshot_raises_rather_than_resolving_what_it_can(
    mutate: object, expected: str
) -> None:
    """A registry that quietly loses entries reads as a smaller corpus.

    Every one of these would otherwise surface as cases skipped for an unknown
    tool, which looks like a dataset that has fewer of that shape rather than
    like an input file that needs regenerating.
    """
    payload = descriptors_to_snapshot([_mcp_descriptor()])
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(RegistrySnapshotError, match=expected):
        snapshot_to_registry(payload)


def test_a_snapshot_that_is_not_json_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(RegistrySnapshotError, match="is not valid JSON"):
        load_registry_snapshot(path)


def test_a_missing_snapshot_file_is_reported_not_ignored(tmp_path: Path) -> None:
    with pytest.raises(RegistrySnapshotError, match="Cannot read snapshot"):
        load_registry_snapshot(tmp_path / "absent.json")


def test_the_envelope_states_its_version() -> None:
    assert descriptors_to_snapshot([])["snapshot_version"] == SNAPSHOT_VERSION
