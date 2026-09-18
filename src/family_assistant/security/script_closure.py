"""Bounded static stored-script discovery and content-only execution bindings.

Only literal ``execute_script(name=...)`` and literal ``tools_execute``
dispatches are discovered. Aliases, computed names and dynamic dispatch must
receive independent runtime review; a static closure is not execution authority.
Bindings describe reviewed content, never trusted provenance or executable
snapshots. Revalidate them against live rows before using parent approval.
"""

from __future__ import annotations

import ast
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.security.definition_records import (
    DefinitionResolution,
    definition_content_hash,
    resolve_definition_record,
    script_definition_content,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.scripts import ScriptRow


class ScriptClosureError(ValueError):
    """A static closure is incomplete, invalid, too large, or stale."""


@dataclass(frozen=True, slots=True)
class ScriptBinding:
    """Serializable content evidence; contains no caller-provided trust stamp."""

    name: str
    description: str
    script_code: str
    parameters_schema: dict[str, object] | None
    content_hash: str

    @classmethod
    def from_row(cls, row: ScriptRow) -> ScriptBinding:
        """Pin a loaded row's effective content without its provenance record."""
        return cls(
            name=row.name,
            description=row.description,
            script_code=row.script_code,
            parameters_schema=deepcopy(row.parameters_schema),
            content_hash=script_content_hash(row),
        )

    def to_dict(self) -> dict[str, object]:
        """Copy content evidence into a JSON-serializable payload."""
        return {
            "name": self.name,
            "description": self.description,
            "script_code": self.script_code,
            "parameters_schema": deepcopy(self.parameters_schema),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class ScriptClosureLimits:
    """Total limits across one closure, including an inline root's source."""

    max_scripts: int = 64
    max_source_bytes: int = 1_048_576
    max_ast_nodes: int = 100_000

    def __post_init__(self) -> None:
        if min(self.max_scripts, self.max_source_bytes, self.max_ast_nodes) <= 0:
            raise ValueError("Script closure limits must be positive")


DEFAULT_SCRIPT_CLOSURE_LIMITS = ScriptClosureLimits()


def _script_content(row: ScriptRow) -> dict[str, object]:
    return script_definition_content(
        name=row.name,
        description=row.description,
        script_code=row.script_code,
        parameters_schema=row.parameters_schema,
    )


def script_content_hash(row: ScriptRow) -> str:
    """Hash all effective definition fields using the stored-record convention."""
    return definition_content_hash(_script_content(row))


@dataclass(frozen=True, slots=True)
class ScriptArtifact:
    """A copied loaded row, its content pin, and independently resolved record."""

    script: ScriptRow
    content_hash: str
    resolution: DefinitionResolution


@dataclass(frozen=True, slots=True)
class ScriptClosure:
    """Deduplicated static descendants and weakest-first stored provenance.

    Only descendants appear here, whether the source is inline or stored.
    A stored root appears only if a literal call reaches it through a cycle.
    Resolve root provenance separately. An empty closure contributes nothing.
    """

    artifacts: tuple[ScriptArtifact, ...]
    resolution: DefinitionResolution | None

    @property
    def bindings(self) -> tuple[ScriptBinding, ...]:
        """Return independent content copies of the reachable stored scripts."""
        return tuple(ScriptBinding.from_row(item.script) for item in self.artifacts)

    def verify_bindings(self, raw: Sequence[object]) -> None:
        """Require exact names, full content and hashes against this closure.

        Resolve this closure from live rows immediately before verification.
        Serialized evidence contains no provenance and cannot grant approval.
        Ordering is immaterial; duplicates, omissions and extra fields fail.
        """
        expected = {binding.name: binding.to_dict() for binding in self.bindings}
        if len(raw) != len(expected):
            raise ScriptClosureError("Stored script closure changed after review")
        seen: set[str] = set()
        for binding in raw:
            if not isinstance(binding, dict):
                raise ScriptClosureError("Invalid script binding")
            name = binding.get("name")
            if not isinstance(name, str) or name in seen:
                raise ScriptClosureError("Invalid or duplicate script binding name")
            if name not in expected or binding != expected[name]:
                raise ScriptClosureError(f"Stored script {name!r} changed after review")
            content = {
                key: value for key, value in binding.items() if key != "content_hash"
            }
            try:
                content_hash = definition_content_hash(content)
            except (TypeError, ValueError, RecursionError) as exc:
                raise ScriptClosureError(
                    f"Invalid script binding for {name!r}"
                ) from exc
            if content_hash != binding["content_hash"]:
                raise ScriptClosureError(f"Invalid script binding hash for {name!r}")
            seen.add(name)


def _literal_string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _static_script_name(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Name):
        return None
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if call.func.id == "tools_execute":
        tool = call.args[0] if call.args else keywords.get("tool_name")
        if _literal_string(tool) != "execute_script":
            return None
    elif call.func.id != "execute_script":
        return None
    return _literal_string(keywords.get("name"))


@dataclass(slots=True)
class _DiscoveryBudget:
    limits: ScriptClosureLimits
    source_bytes: int = 0
    ast_nodes: int = 0

    def dependencies(self, source: str) -> tuple[str, ...]:
        self.source_bytes += len(source.encode("utf-8"))
        if self.source_bytes > self.limits.max_source_bytes:
            raise ScriptClosureError("Script closure exceeds source byte limit")
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ScriptClosureError(
                f"Cannot parse script closure source: {exc}"
            ) from exc
        names: dict[str, None] = {}
        for node in ast.walk(tree):
            self.ast_nodes += 1
            if self.ast_nodes > self.limits.max_ast_nodes:
                raise ScriptClosureError("Script closure exceeds AST node limit")
            if isinstance(node, ast.Call):
                name = _static_script_name(node)
                if name is not None:
                    names[name] = None
        return tuple(names)


async def resolve_script_closure(
    db: Database,
    source: str,
    *,
    loaded_root: ScriptRow | None = None,
    limits: ScriptClosureLimits = DEFAULT_SCRIPT_CLOSURE_LIMITS,
) -> ScriptClosure:
    """Discover literal descendants without executing any source.

    Supply source and optionally its preloaded stored ``loaded_root``.
    The root seeds the row cache but is included only if a literal call reaches
    it. Its provenance otherwise belongs to the caller. Descendants load once
    by name, including in cycles and diamonds. Inline replay consequently
    discovers the same binding names as the original stored invocation.
    Missing static dependencies and exceeded limits raise ScriptClosureError;
    no partial closure is returned. Runtime must recheck pins at child use.
    """
    if loaded_root is not None and loaded_root.script_code != source:
        raise ScriptClosureError("Stored root does not match script closure source")
    budget = _DiscoveryBudget(limits)
    artifacts: list[ScriptArtifact] = []
    discovered: set[str] = set()
    pending: deque[str] = deque()
    resolution: DefinitionResolution | None = None

    def discover(code: str) -> None:
        for name in budget.dependencies(code):
            if name not in discovered:
                discovered.add(name)
                if len(discovered) > limits.max_scripts:
                    raise ScriptClosureError(
                        "Script closure exceeds stored script limit"
                    )
                pending.append(name)

    def include(row: ScriptRow) -> None:
        nonlocal resolution
        pinned = row.model_copy(deep=True)
        content = _script_content(pinned)
        current = resolve_definition_record(pinned.definition_record, content)
        artifacts.append(
            ScriptArtifact(pinned, definition_content_hash(content), current)
        )
        resolution = current if resolution is None else resolution.combine(current)
        if loaded_root is None or pinned.name != loaded_root.name:
            discover(pinned.script_code)

    discover(source)
    while pending:
        name = pending.popleft()
        row = (
            loaded_root
            if loaded_root is not None and name == loaded_root.name
            else await db.scripts.get_by_name(name)
        )
        if row is None:
            raise ScriptClosureError(f"Static script dependency {name!r} not found")
        include(row)
    return ScriptClosure(tuple(artifacts), resolution)
