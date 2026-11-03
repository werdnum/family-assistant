#!/usr/bin/env python3
"""Check that alembic migrations have exactly one head, now and after merging.

Two pull requests that each add a migration on the same parent pass CI separately
and leave main with two heads, which stops the application at startup. `tree`
checks one git tree. `statuses` checks each open pull request merged with the
base branch and reports the result as the `alembic-heads` commit status, which
branch protection requires; run on every push to the base branch, it re-checks
pull requests that CI passed against an older base.

Migration files are read from git objects and parsed with `ast`, never imported,
so a pull request's code is not executed. That is what lets the `statuses` mode
run from `pull_request_target` with a token that can write statuses.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

VERSIONS_DIR = "alembic/versions"
STATUS_CONTEXT = "alembic-heads"
API_ROOT = "https://api.github.com"


@dataclass(frozen=True)
class Migration:
    revision: str
    down_revisions: tuple[str, ...]


def _assigned_value(node: ast.stmt, name: str) -> ast.expr | None:
    if isinstance(node, ast.Assign):
        if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    elif (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == name
    ):
        return node.value
    return None


def _revision_strings(value: ast.expr, path: str) -> tuple[str, ...]:
    if isinstance(value, ast.Constant) and value.value is None:
        return ()
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return (value.value,)
    if isinstance(value, ast.Tuple | ast.List):
        revisions: list[str] = []
        for element in value.elts:
            if not (
                isinstance(element, ast.Constant) and isinstance(element.value, str)
            ):
                break
            revisions.append(element.value)
        else:
            return tuple(revisions)
    raise ValueError(f"{path}: revision identifiers must be string literals")


def parse_migration(source: str, path: str) -> Migration | None:
    """Read `revision` and `down_revision` from a migration's module source."""
    revision: str | None = None
    down_revisions: tuple[str, ...] | None = None
    for node in ast.parse(source, filename=path).body:
        if (value := _assigned_value(node, "revision")) is not None:
            revisions = _revision_strings(value, path)
            if len(revisions) != 1:
                raise ValueError(f"{path}: revision must be a single string")
            revision = revisions[0]
        elif (value := _assigned_value(node, "down_revision")) is not None:
            down_revisions = _revision_strings(value, path)
    if revision is None:
        return None
    if down_revisions is None:
        raise ValueError(f"{path}: has a revision but no down_revision")
    return Migration(revision, down_revisions)


def find_heads(migrations: Iterable[Migration]) -> list[str]:
    migrations = list(migrations)
    parents = {parent for m in migrations for parent in m.down_revisions}
    return sorted(m.revision for m in migrations if m.revision not in parents)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def migrations_in_tree(treeish: str) -> list[Migration]:
    names = _git("ls-tree", "--name-only", f"{treeish}:{VERSIONS_DIR}").split()
    migrations = []
    for name in names:
        if not name.endswith(".py"):
            continue
        path = f"{VERSIONS_DIR}/{name}"
        migration = parse_migration(_git("show", f"{treeish}:{path}"), path)
        if migration is not None:
            migrations.append(migration)
    return migrations


def describe(heads: list[str]) -> tuple[str, str]:
    """The commit status state and description for a set of heads."""
    if len(heads) == 1:
        return "success", f"One alembic head after merging: {heads[0]}"
    if not heads:
        return "failure", "No alembic head after merging (a revision cycle?)"
    return "failure", (
        f"{len(heads)} alembic heads after merging: {', '.join(heads)}. "
        "Merge main and add a merge revision (alembic merge heads)."
    )


def merged_tree(base_ref: str, head_ref: str) -> str | None:
    """The tree of `head_ref` merged into `base_ref`, or None on a conflict."""
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", base_ref, head_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return None
    result.check_returncode()
    return result.stdout.split()[0]


class GitHub:
    def __init__(self, token: str, repository: str) -> None:
        self.token = token
        self.repository = repository

    def request(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> Any:  # noqa: ANN401 -- decoded JSON
        request = urllib.request.Request(
            f"{API_ROOT}/repos/{self.repository}/{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def paginate(self, path: str) -> list[Any]:
        items: list[Any] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            batch = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            assert isinstance(batch, list)
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1


def touches_migrations(github: GitHub, number: int) -> bool:
    files = github.paginate(f"pulls/{number}/files")
    return any(f["filename"].startswith(f"{VERSIONS_DIR}/") for f in files)


def check_pull_request(github: GitHub, base: str, number: int) -> None:
    pull = github.request("GET", f"pulls/{number}")
    head_sha: str = pull["head"]["sha"]
    head_ref = f"refs/remotes/pr/{number}"
    _git(
        "fetch",
        "--quiet",
        "--no-tags",
        "origin",
        f"+refs/pull/{number}/head:{head_ref}",
    )
    tree = merged_tree(f"origin/{base}", head_ref)
    if tree is None:
        state, description = (
            "failure",
            f"Conflicts with {base}; merge {base} to re-check",
        )
    else:
        state, description = describe(find_heads(migrations_in_tree(tree)))
    print(f"#{number} {head_sha[:12]}: {state}: {description}")
    github.request(
        "POST",
        f"statuses/{head_sha}",
        {
            "state": state,
            "context": STATUS_CONTEXT,
            # The API rejects descriptions longer than 140 characters.
            "description": description[:140],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that alembic migrations have exactly one head."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    tree_parser = sub.add_parser("tree", help="check one git tree")
    tree_parser.add_argument("treeish", nargs="?", default="HEAD")
    statuses = sub.add_parser("statuses", help="report pull requests' merged heads")
    statuses.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    statuses.add_argument("--base", default="main")
    statuses.add_argument(
        "--pr",
        type=int,
        action="append",
        help="pull request to check; default: open ones that change migrations",
    )
    statuses.add_argument(
        "--all", action="store_true", help="check every open pull request"
    )
    args = parser.parse_args()

    if args.command == "tree":
        heads = find_heads(migrations_in_tree(args.treeish))
        print("\n".join(heads))
        if len(heads) != 1:
            print(
                f"::error::Expected exactly one alembic head, found {len(heads)}. "
                "Add a merge revision (alembic merge heads).",
                file=sys.stderr,
            )
            return 1
        return 0

    token = os.environ["GITHUB_TOKEN"]
    github = GitHub(token, args.repository)
    numbers = args.pr
    if numbers is None:
        open_pulls = github.paginate(f"pulls?state=open&base={args.base}")
        numbers = [
            p["number"]
            for p in open_pulls
            if args.all or touches_migrations(github, p["number"])
        ]
    for number in numbers:
        check_pull_request(github, args.base, number)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
