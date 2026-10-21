"""The package's module-level imports must form no cycle.

A cycle works or breaks depending on which of its modules a process happens to
import first, so it passes every test that imports the application the usual
way and fails only when a script, a worker or a test imports a module on its
own. This reads the import graph from the source instead of importing anything,
which makes it cheap enough to cover every module.

Imports inside functions and under ``if TYPE_CHECKING:`` do not run at import
time, so they are not edges. Importing ``a.b.c`` also runs ``a/__init__`` and
``a/b/__init__``, so those are edges too, except for the importer's own parent
packages, which have already started initialising before it runs.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
PACKAGE = "family_assistant"


def _module_files() -> dict[str, tuple[Path, bool]]:
    modules: dict[str, tuple[Path, bool]] = {}
    for path in (SRC_ROOT / PACKAGE).rglob("*.py"):
        parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        modules[".".join(parts)] = (path, is_package)
    return modules


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_time_imports(
    body: list[ast.stmt],
) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []
    for node in body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.append(node)
        elif isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                found.extend(_import_time_imports(node.body))
            found.extend(_import_time_imports(node.orelse))
        elif isinstance(node, ast.Try):
            for block in (node.body, node.orelse, node.finalbody):
                found.extend(_import_time_imports(block))
            for handler in node.handlers:
                found.extend(_import_time_imports(handler.body))
        elif isinstance(node, ast.With | ast.ClassDef):
            found.extend(_import_time_imports(node.body))
    return found


def _ancestors(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def _import_graph() -> dict[str, set[str]]:
    modules = _module_files()
    graph: dict[str, set[str]] = {}
    for module, (path, is_package) in modules.items():
        package = module if is_package else module.rpartition(".")[0]
        targets: set[str] = set()
        for node in _import_time_imports(ast.parse(path.read_text()).body):
            if isinstance(node, ast.Import):
                targets.update(alias.name for alias in node.names)
                continue
            if node.level:
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                source = ".".join([*base, *([node.module] if node.module else [])])
            else:
                assert node.module is not None
                source = node.module
            targets.add(source)
            targets.update(
                f"{source}.{alias.name}"
                for alias in node.names
                if f"{source}.{alias.name}" in modules
            )
        already_initialising = {module, *_ancestors(module)}
        graph[module] = {
            dependency
            for target in targets
            for dependency in (*_ancestors(target), target)
            if dependency in modules and dependency not in already_initialising
        }
    return graph


def _find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """Return one import cycle as a path that starts and ends on the same module."""
    done: set[str] = set()
    for root in sorted(graph):
        if root in done:
            continue
        path: list[str] = [root]
        on_path = {root}
        stack = [iter(sorted(graph[root]))]
        while stack:
            dependency = next(stack[-1], None)
            if dependency is None:
                stack.pop()
                finished = path.pop()
                on_path.discard(finished)
                done.add(finished)
                continue
            if dependency in on_path:
                return [*path[path.index(dependency) :], dependency]
            if dependency in done:
                continue
            path.append(dependency)
            on_path.add(dependency)
            stack.append(iter(sorted(graph[dependency])))
    return None


def test_import_graph_is_acyclic() -> None:
    cycle = _find_cycle(_import_graph())
    assert cycle is None, (
        "Import cycle (module-level imports):\n  "
        + "\n  -> ".join(cycle or [])
        + "\nBreak it at a module boundary: import the leaf module that defines"
        " the name rather than a package that re-exports it, or move the shared"
        " piece somewhere both sides can import."
    )


def test_cycle_detection_reports_package_init_cycles() -> None:
    graph = {
        "pkg": {"pkg.leaf", "other"},
        "pkg.leaf": set(),
        "other": {"pkg"},
    }
    assert _find_cycle(graph) == ["other", "pkg", "other"]
