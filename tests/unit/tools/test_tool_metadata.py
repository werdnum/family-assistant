from __future__ import annotations

import ast
import inspect
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
    ToolRegistration,
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


_STAMPING_HELPERS = frozenset({"stamp_definition", "stamp_callback_definition"})


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _stamping_repository_methods() -> set[tuple[str, str]]:
    """(repository class, method) for every repository method that stamps.

    Derived from the repositories themselves: a method whose body calls a
    stamping helper is a definition-write entry point, whatever it is named.
    """
    package_root = Path(family_assistant.__file__).parent
    found: set[tuple[str, str]] = set()
    for path in (package_root / "storage" / "repositories").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for klass in ast.walk(tree):
            if not isinstance(klass, ast.ClassDef):
                continue
            for method in klass.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(
                    isinstance(node, ast.Call) and _call_name(node) in _STAMPING_HELPERS
                    for node in ast.walk(method)
                ):
                    found.add((klass.name, method.name))
    assert found, "Found no repository method that stamps; the scan is broken"
    return found


def _stamping_entry_points() -> set[tuple[str, str]]:
    """(handle attribute, method) a caller writes a definition through.

    The handle attribute is the ``Database`` property that returns the
    repository class, so ``db.events.create_event_listener`` becomes
    ``("events", "create_event_listener")``; ``DatabaseTransaction`` shares
    the properties.
    """
    package_root = Path(family_assistant.__file__).parent
    tree = ast.parse((package_root / "storage" / "database.py").read_text("utf-8"))
    handle_by_class: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(node.returns, ast.Name)
            and any(
                isinstance(d, ast.Name) and d.id == "property"
                for d in node.decorator_list
            )
        ):
            handle_by_class[node.returns.id] = node.name
    entry_points = {
        (handle_by_class[klass], method)
        for klass, method in _stamping_repository_methods()
        if klass in handle_by_class
    }
    assert entry_points, "No stamping repository is exposed on Database; scan broken"
    return entry_points


def _writes_definition(node: ast.AST, entry_points: set[tuple[str, str]]) -> bool:
    """Whether this function body reads the gate or reaches a stamping entry point.

    Nested functions count as part of the function that encloses them.
    """
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr == "definition_gate_outcome"
            and isinstance(child.ctx, ast.Load)
        ):
            return True
        if not isinstance(child, ast.Call):
            continue
        name = _call_name(child)
        if name in _STAMPING_HELPERS:
            return True
        func = child.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and (func.value.attr, func.attr) in entry_points
        ):
            return True
    return False


def _definition_writers(
    registrations: list[ToolRegistration],
) -> set[str]:
    """Names of the registered tools whose implementation writes a definition."""
    entry_points = _stamping_entry_points()
    writers: set[str] = set()
    for registration in registrations:
        implementation = registration.implementation
        source = Path(inspect.getsourcefile(implementation) or "")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        qualname = implementation.__qualname__.split(".")
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == qualname[-1]
                and _writes_definition(node, entry_points)
            ):
                writers.add(registration.name)
    return writers


def test_definition_writers_carry_the_executable_persistence_tag() -> None:
    """A tool that writes an executable definition must resolve to its sink.

    The definition's creation gate only reviews -- and so can only cure -- a
    call whose sink cell adjudicates. A tool is a definition writer when its
    implementation reads the gate outcome or reaches a stamping entry point:
    the stamping helpers themselves, or a repository method that calls them,
    found by scanning the repositories rather than by naming tools. A new
    writer without the tag fails here instead of landing in the audit-only
    ``artifact_write`` cell, and a writer that stamps without consulting the
    gate is caught by the call rather than hidden by the missing read.

    The residual is a tool that reaches a stamping repository through a helper
    in another module; such a write still lands uncured and fires untrusted,
    so the failure is on the safe side.
    """
    registrations = [*LOCAL_TOOL_REGISTRATIONS, *plugin_tool_registrations()]
    writers = _definition_writers(registrations)
    assert writers, "Found no definition writer; the scan is broken"
    tagged = {
        registration.name
        for registration in registrations
        if ToolTag.EXECUTABLE_PERSISTENCE in registration.tags
    }
    assert writers == tagged


def test_definition_writers_resolve_to_executable_persistence() -> None:
    """Every definition writer reaches a cell that adjudicates at external tiers.

    A writer that also carries ``code_execution`` (``save_script``,
    ``spawn_worker``) resolves to ``sandbox_network`` instead, whose cell
    adjudicates at the same tiers, so it is gated either way.
    """
    writers = [
        descriptor
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if ToolTag.EXECUTABLE_PERSISTENCE in descriptor.tags
    ]
    sandboxed = {
        descriptor.name: resolve_tool_sink_class(descriptor)
        for descriptor in writers
        if ToolTag.CODE_EXECUTION in descriptor.tags
    }
    persisted = {
        descriptor.name: resolve_tool_sink_class(descriptor)
        for descriptor in writers
        if ToolTag.CODE_EXECUTION not in descriptor.tags
    }
    assert sandboxed and persisted
    assert set(sandboxed.values()) == {SinkClass.SANDBOX_NETWORK}
    assert set(persisted.values()) == {SinkClass.EXECUTABLE_PERSISTENCE}

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
