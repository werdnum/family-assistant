"""Unit tests for the tool-registry snapshot the review eval resolves against."""

from __future__ import annotations

import importlib.util
import json
import sys
from typing import TYPE_CHECKING

import pytest

from family_assistant.eval.tool_call_review.registry_snapshot import (
    DESCRIPTOR_FIELDS,
    SNAPSHOT_VERSION,
    RegistrySnapshotError,
    descriptors_to_snapshot,
    load_registry_snapshot,
    registry_digest,
    snapshot_to_registry,
)
from family_assistant.eval.tool_call_review.scrub import (
    TaskTemplate,
    TemplatePrivacyError,
    declared_argument_shapes,
)
from family_assistant.paths import PROJECT_ROOT
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.google_data import GOOGLE_TOOL_REQUIRED_SCOPES
from family_assistant.tools.metadata import ToolDescriptor, ToolTag

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

pytestmark = pytest.mark.no_db


def _mcp_descriptor(
    name: str = "plan_trip", server_id: str = "transport"
) -> ToolDescriptor:
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
        mcp_server_id=server_id,
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
            lambda payload: payload["tools"]["plan_trip"].__setitem__(
                "definition", "not-an-object"
            ),
            "no tool definition object",
            id="definition-not-an-object",
        ),
        pytest.param(
            lambda payload: payload["tools"]["plan_trip"].__setitem__("definition", {}),
            "definition with no function",
            id="definition-empty",
        ),
        pytest.param(
            lambda payload: payload["tools"]["plan_trip"]["definition"][
                "function"
            ].__setitem__("name", "something_else"),
            "the key and the definition must agree",
            id="name-disagrees-with-key",
        ),
        pytest.param(
            lambda payload: payload["tools"]["plan_trip"]["definition"]["function"].pop(
                "parameters"
            ),
            "declares no parameter schema",
            id="no-parameter-schema",
        ),
        pytest.param(
            lambda payload: payload["tools"]["plan_trip"].__setitem__(
                "mcp_server_id", None
            ),
            "origin 'mcp' but no mcp_server_id",
            id="mcp-without-a-server",
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


@pytest.mark.parametrize("omitted", DESCRIPTOR_FIELDS)
def test_every_omitted_descriptor_field_is_malformed_not_a_default(
    omitted: str,
) -> None:
    """Absent is never empty, for any field -- stated as a rule, not a list.

    Several of these have an empty value that is also their default, which is
    exactly when a missing key loads quietly and changes the reviewer input:
    ``destination_argument_paths`` drops the trusted-destination echo and
    ``mcp_server_id`` renders a null server into the tool context. Parametrizing
    over the declared field tuple rather than a hand-written subset is the
    point: a field added to the serializer is covered here without anyone
    remembering to add it.
    """
    payload = descriptors_to_snapshot([_mcp_descriptor()])
    del payload["tools"]["plan_trip"][omitted]  # type: ignore[index]

    with pytest.raises(RegistrySnapshotError, match=f"is missing {omitted}"):
        snapshot_to_registry(payload)


def test_the_serializer_and_the_reader_agree_on_the_field_set() -> None:
    """The writer refuses to emit an entry the reader would reject.

    Both sides read ``DESCRIPTOR_FIELDS``; this pins that they cannot drift
    apart silently if someone edits one of them.
    """
    entry = descriptors_to_snapshot([_mcp_descriptor()])["tools"]["plan_trip"]  # type: ignore[index]

    assert set(entry) == set(DESCRIPTOR_FIELDS)


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


def test_a_connected_server_that_advertises_nothing_aborts_the_dump() -> None:
    """Connected is not contributed, and only one of them is the point.

    ``MCPToolsProvider`` marks a session connected before it lists tools, so a
    server that comes back with an empty list stays connected and advertises
    nothing. That reaches a snapshot as the server's whole surface missing --
    the same silent, biased corpus this script exists to prevent.
    """
    script = _load_dump_registry_script()
    configs = {"transport": {}, "brave": {}}
    statuses = {
        "transport": {"status": "connected"},
        "brave": {"status": "connected"},
    }

    script._require_every_server_contributed(
        configs,
        statuses,
        [_mcp_descriptor(), _mcp_descriptor("brave_web_search", "brave")],
    )

    with pytest.raises(SystemExit, match="brave"):
        script._require_every_server_contributed(configs, statuses, [_mcp_descriptor()])


def test_a_server_that_did_not_connect_aborts_the_dump() -> None:
    script = _load_dump_registry_script()

    with pytest.raises(SystemExit, match="transport"):
        script._require_every_server_contributed(
            {"transport": {}}, {"transport": {"status": "failed"}}, []
        )


def _load_dump_registry_script() -> ModuleType:
    """Load the dump script by path; ``scripts/`` is not an importable package."""
    script_path = PROJECT_ROOT / "scripts" / "dump_tool_registry.py"
    spec = importlib.util.spec_from_file_location(
        "dump_tool_registry_script", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_dump_reads_the_overlay_the_deployment_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot from a different config describes a registry nobody has.

    ``family_assistant.__main__`` resolves the overlay as ``$CONFIG_FILE`` with
    ``config.yaml`` as the fallback; reading a different file would connect to
    servers the deployment disabled, or omit the ones it added.
    """
    script = _load_dump_registry_script()
    overlay = tmp_path / "deployment.yaml"
    overlay.write_text(
        "mcp_config:\n  mcpServers:\n    transport:\n      command: 'x'\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CONFIG_FILE", str(overlay))
    config = script._load_deployment_config(None)
    assert "transport" in script._configured_servers(config)

    monkeypatch.delenv("CONFIG_FILE")
    config = script._load_deployment_config(str(overlay))
    assert "transport" in script._configured_servers(config)


def test_a_run_records_which_registry_it_measured_under() -> None:
    """Two snapshots over one dataset are two measurements, not one.

    Tags, destination paths and the MCP server id all render into the reviewer's
    tool context, so a run that recorded only the dataset hash would stamp
    identically to one measured against different tool definitions.
    """
    one = snapshot_to_registry(descriptors_to_snapshot([_mcp_descriptor()]))
    other = snapshot_to_registry(
        descriptors_to_snapshot([_mcp_descriptor(server_id="other-server")])
    )

    assert registry_digest(one) != registry_digest(other)
    assert registry_digest(one) == registry_digest(dict(one))
    assert registry_digest(None) is None


def test_a_named_overlay_that_is_not_there_aborts_the_dump(tmp_path: Path) -> None:
    """A typo'd --config-file must not silently become the shipped defaults.

    `load_config` treats every overlay as optional, so a path that is not there
    is skipped and the dump writes a plausible snapshot of a server set no
    deployment runs.
    """
    script = _load_dump_registry_script()

    with pytest.raises(SystemExit, match="does not exist"):
        script._load_deployment_config(str(tmp_path / "typo.yaml"))


def test_a_named_env_overlay_that_is_not_there_aborts_the_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`$CONFIG_FILE` names an overlay as much as `--config-file` does.

    It is also the documented deployment path, so guarding only the flag guards
    the route nobody uses in production. What makes a missing overlay an error
    is that someone named it, not which mechanism they named it with; only the
    fallback stays optional, because a deployment with no overlay is ordinary.
    """
    script = _load_dump_registry_script()

    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "typo.yaml"))
    with pytest.raises(SystemExit, match=r"\$CONFIG_FILE.*does not exist"):
        script._load_deployment_config(None)

    monkeypatch.delenv("CONFIG_FILE")
    script._load_deployment_config(None)


def test_local_only_dump_uses_the_deployment_effective_registry(
    tmp_path: Path,
) -> None:
    """Even without MCP, a snapshot must describe this deployment's schemas."""
    script = _load_dump_registry_script()
    overlay = tmp_path / "deployment.yaml"
    overlay.write_text(
        "ai_worker_config:\n  available_agents: [codex]\n", encoding="utf-8"
    )
    snapshot_path = tmp_path / "registry.json"

    result = script.main([
        "--config-file",
        str(overlay),
        "--local-only",
        "--out",
        str(snapshot_path),
        "--allow-external-out",
    ])

    registry = load_registry_snapshot(snapshot_path)
    worker = registry["spawn_worker"]
    assert result == 0
    assert worker.definition["function"]["parameters"]["properties"]["agent"][  # type: ignore[index]
        "enum"
    ] == ["codex"]
    assert set(registry).isdisjoint(GOOGLE_TOOL_REQUIRED_SCOPES)
