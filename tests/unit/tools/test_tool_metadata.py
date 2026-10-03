from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import family_assistant
from family_assistant.plugins.registry import plugin_tool_registrations
from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyEvaluator,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    resolve_tool_sink_class,
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS,
    LOCAL_TOOL_DESCRIPTORS,
    LOCAL_TOOL_METADATA_BY_NAME,
    LOCAL_TOOL_REGISTRATIONS,
    TOOLS_DEFINITION,
)
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    OUTPUT_TRUST_TAGS,
    ToolTag,
    build_local_tool_registrations,
    derive_mcp_annotation_tags,
    make_local_tool_metadata,
    normalize_mcp_tool_metadata,
    resolve_mcp_tool_tags,
    uncovered_configured_tools,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition


def test_local_tool_catalog_has_complete_metadata_coverage() -> None:
    """Every local tool in the exported catalog should have metadata and descriptors."""
    definition_names = [tool["function"]["name"] for tool in TOOLS_DEFINITION]
    registration_names = [
        registration.name for registration in LOCAL_TOOL_REGISTRATIONS
    ]
    descriptor_names = [descriptor.name for descriptor in LOCAL_TOOL_DESCRIPTORS]

    assert (
        len(TOOLS_DEFINITION)
        == len(AVAILABLE_FUNCTIONS)
        == len(LOCAL_TOOL_REGISTRATIONS)
    )
    assert definition_names == registration_names == descriptor_names
    assert set(LOCAL_TOOL_METADATA_BY_NAME) == set(definition_names)
    assert all(registration.tags for registration in LOCAL_TOOL_REGISTRATIONS)

    descriptor_map = {
        descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS
    }
    assert ToolTag.STATE_PERSISTING in descriptor_map["add_or_update_note"].tags
    assert ToolTag.DELEGATION in descriptor_map["delegate_to_service"].tags
    assert ToolTag.CODE_EXECUTION in descriptor_map["execute_script"].tags


def test_every_registered_tool_declares_output_trust() -> None:
    """Every core and plugin tool must say how far its output is trusted.

    A tool without an output trust tag falls back to a runtime default with a
    warning, so a new tool's taint grade would be decided by accident.
    """
    registrations = [*LOCAL_TOOL_REGISTRATIONS, *plugin_tool_registrations()]
    missing = sorted(
        registration.name
        for registration in registrations
        if not registration.tags & OUTPUT_TRUST_TAGS
    )
    assert not missing, (
        "Tools missing an output trust tag "
        f"({', '.join(sorted(OUTPUT_TRUST_TAGS))}): {missing}"
    )


def _collect_gate_readers(
    node: ast.AST,
    scope: tuple[str, ...],
    readers: set[tuple[str, ...]],
    *,
    in_function: bool = False,
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef) or (
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not in_function
        ):
            _collect_gate_readers(
                child,
                (*scope, child.name),
                readers,
                in_function=not isinstance(child, ast.ClassDef),
            )
            continue
        if (
            isinstance(child, ast.Attribute)
            and child.attr == "definition_gate_outcome"
            and isinstance(child.ctx, ast.Load)
            and scope
        ):
            readers.add(scope)
        _collect_gate_readers(child, scope, readers, in_function=in_function)


def _functions_reading_definition_gate() -> set[tuple[str, str]]:
    """Module and qualified name of every function that reads the gate outcome.

    A read inside a nested function belongs to the function that encloses it.
    The gating wrapper in ``tools/infrastructure.py`` deposits the outcome, so
    it is the one reader excluded.
    """
    package_root = Path(family_assistant.__file__).parent
    readers: set[tuple[str, str]] = set()
    for path in package_root.rglob("*.py"):
        if path == package_root / "tools" / "infrastructure.py":
            continue
        module = ".".join((
            "family_assistant",
            *path.relative_to(package_root).with_suffix("").parts,
        ))
        scopes: set[tuple[str, ...]] = set()
        _collect_gate_readers(ast.parse(path.read_text(encoding="utf-8")), (), scopes)
        readers.update((module, ".".join(scope)) for scope in scopes)
    return readers


def test_definition_writers_carry_the_executable_persistence_tag() -> None:
    """A tool that writes an executable definition must resolve to its sink.

    The definition's creation gate only reviews -- and so can only cure -- a
    call whose sink cell adjudicates. Reading the gate outcome is what makes a
    tool a definition writer, so the tag is tied to that read rather than to a
    list of tool names: a new writer without the tag fails here instead of
    landing in the audit-only ``artifact_write`` cell.
    """
    registrations = [*LOCAL_TOOL_REGISTRATIONS, *plugin_tool_registrations()]
    by_function = {
        (
            registration.implementation.__module__,
            registration.implementation.__qualname__,
        ): registration
        for registration in registrations
    }
    readers = _functions_reading_definition_gate()
    assert readers, "Found no reader of definition_gate_outcome; the scan is broken"

    unregistered = sorted(".".join(reader) for reader in readers - by_function.keys())
    assert not unregistered, (
        "definition_gate_outcome must be read by the registered tool implementation "
        f"itself, not a helper, so its tag can be checked: {unregistered}"
    )
    writers = {by_function[reader].name for reader in readers}
    tagged = {
        registration.name
        for registration in registrations
        if ToolTag.EXECUTABLE_PERSISTENCE in registration.tags
    }
    assert writers == tagged


def test_definition_writers_resolve_to_executable_persistence() -> None:
    """Every definition writer reaches a cell that adjudicates at external tiers.

    ``save_script`` also carries ``code_execution``, whose ``sandbox_network``
    cell adjudicates at the same tiers, so it is gated either way.
    """
    descriptors = {descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS}
    resolved = {
        descriptor.name: resolve_tool_sink_class(descriptor)
        for descriptor in descriptors.values()
        if ToolTag.EXECUTABLE_PERSISTENCE in descriptor.tags
    }
    assert resolved.pop("save_script") is SinkClass.SANDBOX_NETWORK
    assert resolved
    assert set(resolved.values()) == {SinkClass.EXECUTABLE_PERSISTENCE}

    evaluator = TaintPolicyEvaluator(TaintPolicyConfig())
    for tier in (
        SourceTrustTier.KNOWN_CONTACT,
        SourceTrustTier.RECOGNIZED_MACHINE,
        SourceTrustTier.UNKNOWN_EXTERNAL,
    ):
        state = TurnTaintState.empty().add_source(
            TaintSource(
                source_type=TaintSourceType.TOOL_OUTPUT,
                source_id="external",
                tier=tier,
                labels=frozenset(),
                reason="External content in the authoring turn.",
            )
        )
        evaluation = evaluator.evaluate(
            state=state, sink_class=SinkClass.EXECUTABLE_PERSISTENCE
        )
        assert evaluation.requested_outcome is TaintPolicyOutcome.ADJUDICATE, tier
        assert evaluation.fallback_outcome is TaintPolicyOutcome.CONFIRM, tier

    clean = evaluator.evaluate(
        state=TurnTaintState.empty(), sink_class=SinkClass.EXECUTABLE_PERSISTENCE
    )
    assert clean.requested_outcome is TaintPolicyOutcome.ALLOW


def test_build_local_tool_registrations_rejects_missing_metadata() -> None:
    """Registration building should fail closed when metadata is missing."""

    async def example_tool() -> str:
        return "ok"

    definitions: list[ToolDefinition] = [
        {
            "type": "function",
            "function": {
                "name": "example_tool",
                "description": "Example tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    with pytest.raises(ValueError, match="Missing local tool metadata"):
        build_local_tool_registrations(
            definitions=definitions,
            implementations={"example_tool": example_tool},
            metadata_by_name={},
        )


def test_resolve_mcp_tool_tags_prefers_config_then_wildcard_then_annotations() -> None:
    """MCP metadata resolution should follow exact, wildcard, then annotations."""
    annotation_tags = derive_mcp_annotation_tags(
        read_only_hint=True,
        destructive_hint=None,
        open_world_hint=True,
    )
    tool_metadata = normalize_mcp_tool_metadata({
        "search_web": ["low_bandwidth_external", "output_untrusted"],
        "*": ["read_only", "output_trusted"],
    })

    exact_tags = resolve_mcp_tool_tags(
        tool_name="search_web",
        configured_tool_metadata=tool_metadata,
        annotation_tags=annotation_tags,
    )
    wildcard_tags = resolve_mcp_tool_tags(
        tool_name="get_time",
        configured_tool_metadata=tool_metadata,
        annotation_tags=annotation_tags,
    )
    annotation_only_tags = resolve_mcp_tool_tags(
        tool_name="no_config",
        configured_tool_metadata=None,
        annotation_tags=annotation_tags,
    )

    assert exact_tags == {
        ToolTag.LOW_BANDWIDTH_EXTERNAL,
        ToolTag.OUTPUT_UNTRUSTED,
    }
    assert wildcard_tags == {ToolTag.READ_ONLY, ToolTag.OUTPUT_TRUSTED}
    assert annotation_only_tags == {
        ToolTag.READ_ONLY,
        ToolTag.OPEN_WORLD,
        ToolTag.OUTPUT_UNTRUSTED,
    }


def test_resolve_mcp_tool_tags_adds_output_unspecified_when_annotations_lack_output() -> (
    None
):
    """Annotation fallback should still mark output safety as unspecified when needed."""
    annotation_tags = derive_mcp_annotation_tags(
        read_only_hint=True,
        destructive_hint=None,
        open_world_hint=None,
    )

    # openWorldHint defaults to true in the MCP spec, so a read-only tool that
    # does not explicitly close its world is marked OPEN_WORLD.
    assert resolve_mcp_tool_tags(
        tool_name="read_only_tool",
        configured_tool_metadata=None,
        annotation_tags=annotation_tags,
    ) == {ToolTag.READ_ONLY, ToolTag.OPEN_WORLD, ToolTag.OUTPUT_UNSPECIFIED}


def test_resolve_mcp_tool_tags_adds_output_unspecified_when_config_lacks_output() -> (
    None
):
    """Operator-configured tags without an output tag are marked unspecified."""
    tool_metadata = normalize_mcp_tool_metadata({
        "search_web": ["read_only"],
        "*": ["state_changing"],
    })
    annotation_tags = derive_mcp_annotation_tags(
        read_only_hint=True,
        destructive_hint=None,
        open_world_hint=True,
    )

    assert resolve_mcp_tool_tags(
        tool_name="search_web",
        configured_tool_metadata=tool_metadata,
        annotation_tags=annotation_tags,
    ) == {ToolTag.READ_ONLY, ToolTag.OUTPUT_UNSPECIFIED}
    assert resolve_mcp_tool_tags(
        tool_name="other",
        configured_tool_metadata=tool_metadata,
        annotation_tags=annotation_tags,
    ) == {ToolTag.STATE_CHANGING, ToolTag.OUTPUT_UNSPECIFIED}


def test_uncovered_configured_tools_reports_tools_an_exact_map_omits() -> None:
    """A map with no wildcard must cover every tool or say which it does not."""
    configured = normalize_mcp_tool_metadata({
        "airbnb_property": ["read_only", "output_untrusted"],
    })

    assert uncovered_configured_tools(
        ["airbnb_property", "booking", "booking_property"], configured
    ) == ("booking", "booking_property")
    # A wildcard covers a tool the map does not name.
    assert (
        uncovered_configured_tools(
            ["airbnb_property", "booking"],
            normalize_mcp_tool_metadata({"airbnb_property": ["read_only"], "*": []}),
        )
        == ()
    )
    # No configured map at all is not a declared-coverage gap.
    assert uncovered_configured_tools(["anything"], {}) == ()


def test_derive_mcp_annotation_tags_marks_open_world_by_default() -> None:
    """A read-only tool without explicit openWorldHint=false is open-world."""
    # openWorldHint=None (unset) -> conservative open-world default.
    assert derive_mcp_annotation_tags(
        read_only_hint=True,
        destructive_hint=None,
        open_world_hint=None,
    ) == frozenset({ToolTag.READ_ONLY, ToolTag.OPEN_WORLD})
    # openWorldHint=True -> open-world plus untrusted output.
    assert derive_mcp_annotation_tags(
        read_only_hint=True,
        destructive_hint=None,
        open_world_hint=True,
    ) == frozenset({
        ToolTag.READ_ONLY,
        ToolTag.OPEN_WORLD,
        ToolTag.OUTPUT_UNTRUSTED,
    })


def test_derive_mcp_annotation_tags_closed_world_read_only_has_no_open_world() -> None:
    """An explicit openWorldHint=false clears the open-world marker."""
    assert derive_mcp_annotation_tags(
        read_only_hint=True,
        destructive_hint=None,
        open_world_hint=False,
    ) == frozenset({ToolTag.READ_ONLY})


def test_derive_mcp_annotation_tags_open_world_requires_read_only() -> None:
    """OPEN_WORLD is only derived alongside a read-only hint."""
    # A non-read-only open-world tool already carries an egress-worthy sink
    # classification via its other tags, so the marker is not added here.
    assert derive_mcp_annotation_tags(
        read_only_hint=None,
        destructive_hint=None,
        open_world_hint=True,
    ) == frozenset({ToolTag.OUTPUT_UNTRUSTED})


@pytest.mark.asyncio
async def test_local_tools_provider_exposes_descriptors_when_built_from_registrations() -> (
    None
):
    """Providers built from registrations should expose descriptors alongside definitions."""

    async def example_tool() -> str:
        return "ok"

    definitions: list[ToolDefinition] = [
        {
            "type": "function",
            "function": {
                "name": "example_tool",
                "description": "Example tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    registrations = build_local_tool_registrations(
        definitions=definitions,
        implementations={"example_tool": example_tool},
        metadata_by_name={
            "example_tool": make_local_tool_metadata([
                ToolTag.READ_ONLY,
                ToolTag.OUTPUT_TRUSTED,
            ])
        },
    )
    provider = LocalToolsProvider(registrations=registrations)

    descriptor = await provider.get_tool_descriptor("example_tool")

    assert descriptor is not None
    assert descriptor.origin == "local"
    assert descriptor.tags == {ToolTag.READ_ONLY, ToolTag.OUTPUT_TRUSTED}
