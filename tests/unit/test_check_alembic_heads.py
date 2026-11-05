"""Tests for the alembic head check that guards against parallel migrations."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_alembic_heads.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_alembic_heads", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


heads = _script()


def _migration(revision: str, down: str) -> str:
    return (
        "from collections.abc import Sequence\n"
        f'revision: str = "{revision}"\n'
        f"down_revision: str | Sequence[str] | None = {down}\n"
        "def upgrade() -> None:\n    raise RuntimeError('must not run')\n"
    )


def test_parses_annotated_plain_and_tuple_down_revisions() -> None:
    assert heads.parse_migration(_migration("b", '"a"'), "b.py") == heads.Migration(
        "b", ("a",)
    )
    assert heads.parse_migration(_migration("a", "None"), "a.py") == heads.Migration(
        "a", ()
    )
    assert heads.parse_migration(
        'revision = "m"\ndown_revision = (\n    "b",\n    "c",\n)\n', "m.py"
    ) == heads.Migration("m", ("b", "c"))


def test_rejects_computed_revision_identifiers() -> None:
    with pytest.raises(ValueError, match="string literals"):
        heads.parse_migration('revision = "b"\ndown_revision = PARENT\n', "b.py")


def test_module_without_revision_is_ignored() -> None:
    assert heads.parse_migration("HELPER = 1\n", "helpers.py") is None


def test_two_children_of_one_parent_are_two_heads_until_merged() -> None:
    migrations = [
        heads.Migration("a", ()),
        heads.Migration("b", ("a",)),
        heads.Migration("c", ("a",)),
    ]
    assert heads.find_heads(migrations) == ["b", "c"]
    assert heads.find_heads([*migrations, heads.Migration("m", ("b", "c"))]) == ["m"]


def test_describe_reports_heads() -> None:
    assert heads.describe(["m"])[0] == "success"
    state, description = heads.describe(["b", "c"])
    assert state == "failure"
    assert "b, c" in description


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, content in files.items():
        path = repo / "alembic" / "versions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def test_merged_tree_of_parallel_migration_branches_has_two_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit(repo, {"a.py": _migration("a", "None")}, "base")
    _git(repo, "branch", "other")
    _commit(repo, {"b.py": _migration("b", '"a"')}, "first PR")
    _git(repo, "checkout", "-q", "other")
    _commit(repo, {"c.py": _migration("c", '"a"')}, "second PR")
    monkeypatch.chdir(repo)

    assert heads.find_heads(heads.migrations_in_tree("main")) == ["b"]
    assert heads.find_heads(heads.migrations_in_tree("other")) == ["c"]
    tree = heads.merged_tree("main", "other")
    assert tree is not None
    assert heads.find_heads(heads.migrations_in_tree(tree)) == ["b", "c"]
