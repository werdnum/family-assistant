#!/usr/bin/env python3
"""Push local commits to GitHub through the REST API instead of git.

Mounted into the Antigravity sandbox when its egress rule for api.github.com
carries a stored, rotating credential. Git over HTTPS cannot use that
credential -- GitHub's git endpoint accepts only Basic auth, and the store only
sends Bearer -- so a git push stops working once the submit-time token expires,
about an hour into a run. The REST API takes the rotating Bearer token, so this
replays each local commit through the Git Data API (blobs, trees, commits, then
the branch ref) and works for as long as the run does.

It sends no credential of its own: the sandbox's egress proxy attaches one on
the way out. Standard library only, because the sandbox is a fresh machine.

Usage, from inside the repository:

    python3 github_push.py BRANCH [--remote origin] [--force]

Replays every commit reachable from HEAD that the remote does not already have,
in order, then points BRANCH at the result. Trees are content-addressed, and a
commit carries its original author, committer, dates and message, so the pushed
commits normally keep their local ids; where one does not, later commits are
re-parented onto what GitHub created and the difference is reported.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_URL = os.environ.get("FA_GITHUB_API_URL", "https://api.github.com").rstrip("/")
_REMOTE_PATTERN = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)
_IDENTITY_PATTERN = re.compile(
    r"^(?P<name>.*) <(?P<email>[^>]*)> (?P<seconds>-?\d+) (?P<tz>[+-]\d{4})$"
)
_SUBMODULE_MODE = "160000"


class PushError(Exception):
    """A push that cannot continue; the message says why."""


class EmptyRepositoryError(PushError):
    """GitHub's API cannot write to a repository that has no commits yet."""


def git(*args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise PushError(
            f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout


def succeeds(*args: str) -> bool:
    """Whether a git command exits cleanly -- for git's yes/no questions."""
    return (
        subprocess.run(["git", *args], capture_output=True, check=False).returncode == 0
    )


def git_text(*args: str) -> str:
    return git(*args).decode().strip()


def api(method: str, path: str, body: object | None = None) -> object | None:
    """Call the GitHub API; ``None`` for a 404, raise for any other failure."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return None
        detail = e.read().decode(errors="replace")
        if e.code == 409 and "empty" in detail.lower():
            raise EmptyRepositoryError(detail) from e
        hint = ""
        if e.code == 401:
            hint = (
                " (no credential reached GitHub: the sandbox's egress rule for "
                "api.github.com must carry the github_app bearer credential)"
            )
        raise PushError(f"{method} {path} -> {e.code}{hint}: {detail}") from e


def repository_path(remote: str) -> str:
    url = git_text("remote", "get-url", remote)
    match = _REMOTE_PATTERN.search(url)
    if not match:
        raise PushError(f"remote {remote!r} is not a GitHub repository: {url}")
    return f"/repos/{match['owner']}/{match['name']}"


def identity(line: str) -> dict[str, str]:
    """Turn a commit header's ``Name <email> seconds +hhmm`` into API form."""
    match = _IDENTITY_PATTERN.match(line)
    if not match:
        raise PushError(f"cannot parse commit identity {line!r}")
    tz = match["tz"]
    offset = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
    zone = timezone(-offset if tz[0] == "-" else offset)
    when = datetime.fromtimestamp(int(match["seconds"]), zone)
    return {"name": match["name"], "email": match["email"], "date": when.isoformat()}


def parse_commit(sha: str) -> tuple[dict[str, str], dict[str, str], str]:
    """Author, committer and message of a local commit, exactly as stored."""
    raw = git("cat-file", "commit", sha).decode()
    header, _, message = raw.partition("\n\n")
    fields: dict[str, str] = {}
    for line in header.split("\n"):
        key, _, value = line.partition(" ")
        if key in {"author", "committer"}:
            fields[key] = value
        elif key == "gpgsig":
            print(
                f"note: {sha[:12]} is signed locally; the pushed copy is not, so "
                "its id will differ",
                file=sys.stderr,
            )
    return identity(fields["author"]), identity(fields["committer"]), message


class Pusher:
    """Replays local commits onto one GitHub repository."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.uploaded: dict[str, str] = {}
        # Local commit id -> the id GitHub gave it, where the two differ.
        self.remote_ids: dict[str, str] = {}

    def tree_entries(self, parent: str | None, commit: str) -> list[dict[str, object]]:
        """The tree changes that turn ``parent`` into ``commit``, blobs uploaded."""
        if parent is None:
            listing = git("ls-tree", "-r", "-z", commit).decode().split("\0")
            changes = []
            for item in filter(None, listing):
                meta, path = item.split("\t", 1)
                mode, _kind, sha = meta.split(" ")
                changes.append(("A", mode, sha, path))
        else:
            raw = git(
                "diff-tree",
                "-r",
                "-z",
                "--no-renames",
                "--no-commit-id",
                parent,
                commit,
            ).decode()
            fields = raw.split("\0")
            changes = []
            for index in range(0, len(fields) - 1, 2):
                meta, path = fields[index], fields[index + 1]
                old_mode, new_mode, _old_sha, new_sha, status = meta.lstrip(":").split(
                    " "
                )
                changes.append((
                    status,
                    old_mode if status == "D" else new_mode,
                    new_sha,
                    path,
                ))

        entries: list[dict[str, object]] = []
        for status, mode, sha, path in changes:
            if status == "D":
                kind = "commit" if mode == _SUBMODULE_MODE else "blob"
                entries.append({"path": path, "mode": mode, "type": kind, "sha": None})
            elif mode == _SUBMODULE_MODE:
                entries.append({
                    "path": path,
                    "mode": mode,
                    "type": "commit",
                    "sha": sha,
                })
            else:
                entries.append({
                    "path": path,
                    "mode": mode,
                    "type": "blob",
                    "sha": self.upload_blob(sha),
                })
        return entries

    def upload_blob(self, sha: str) -> str:
        if sha not in self.uploaded:
            content = base64.b64encode(git("cat-file", "blob", sha)).decode()
            created = api(
                "POST",
                f"{self.repo}/git/blobs",
                {"content": content, "encoding": "base64"},
            )
            assert isinstance(created, dict)
            self.uploaded[sha] = str(created["sha"])
        return self.uploaded[sha]

    def push_commit(self, commit: str) -> str:
        """Recreate one local commit on GitHub and return the id GitHub gave it."""
        parents = git_text("rev-list", "--parents", "-n", "1", commit).split()[1:]
        local_tree = git_text("rev-parse", f"{commit}^{{tree}}")
        first = parents[0] if parents else None
        entries = self.tree_entries(first, commit)
        if first is not None and not entries:
            tree = git_text("rev-parse", f"{first}^{{tree}}")
        else:
            body: dict[str, object] = {"tree": entries}
            if first is not None:
                body["base_tree"] = git_text("rev-parse", f"{first}^{{tree}}")
            created = api("POST", f"{self.repo}/git/trees", body)
            assert isinstance(created, dict)
            tree = str(created["sha"])
        if tree != local_tree:
            raise PushError(
                f"GitHub built tree {tree} for {commit[:12]}, but the local tree is "
                f"{local_tree}; stopping rather than pushing different content"
            )

        author, committer, message = parse_commit(commit)
        created = api(
            "POST",
            f"{self.repo}/git/commits",
            {
                "message": message,
                "tree": tree,
                "parents": [self.remote_ids.get(parent, parent) for parent in parents],
                "author": author,
                "committer": committer,
            },
        )
        assert isinstance(created, dict)
        return str(created["sha"])


def push_with_git(remote: str, branch: str) -> int:
    """The first push to an empty repository, which only git can make."""
    print(
        "the repository is empty, which GitHub's API cannot write to; "
        "making the first push with git",
        file=sys.stderr,
    )
    result = subprocess.run(
        ["git", "push", remote, f"HEAD:refs/heads/{branch}"], check=False
    )
    if result.returncode != 0:
        raise PushError(
            "the first push to an empty repository has to go through git, whose "
            "credential stops working about an hour into the task; once the "
            "repository has a commit, this helper can push for the rest of it"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push local commits to GitHub through the REST API."
    )
    parser.add_argument("branch")
    parser.add_argument("--remote", default="origin")
    parser.add_argument(
        "--force",
        action="store_true",
        help="move the branch even if not a fast-forward",
    )
    args = parser.parse_args()

    repo = repository_path(args.remote)
    try:
        return push(repo, args.remote, args.branch, force=args.force)
    except EmptyRepositoryError:
        return push_with_git(args.remote, args.branch)


def push(repo: str, remote: str, branch: str, *, force: bool) -> int:
    pusher = Pusher(repo)
    head = git_text("rev-parse", "HEAD")
    quoted = urllib.parse.quote(branch, safe="/")
    ref = api("GET", f"{repo}/git/ref/heads/{quoted}")
    remote_head = str(ref["object"]["sha"]) if isinstance(ref, dict) else None
    if remote_head == head:
        print(f"{branch} is already at {head[:12]}")
        return 0

    known_locally = remote_head is not None and succeeds(
        "cat-file", "-e", f"{remote_head}^{{commit}}"
    )
    fast_forward = known_locally and succeeds(
        "merge-base", "--is-ancestor", str(remote_head), head
    )
    if remote_head is not None and not force and not fast_forward:
        raise PushError(
            f"remote {branch} is at {remote_head[:12]}, which HEAD does not "
            "contain; integrate it first or pass --force"
        )

    exclude = [f"--remotes={remote}"]
    if known_locally and remote_head is not None:
        exclude.append(remote_head)
    commits = git_text(
        "rev-list", "--reverse", "--topo-order", head, "--not", *exclude
    ).split()

    for commit in commits:
        pushed = pusher.push_commit(commit)
        if pushed != commit:
            pusher.remote_ids[commit] = pushed
        print(
            f"pushed {commit[:12]}" + (f" as {pushed[:12]}" if pushed != commit else "")
        )
    new_head = pusher.remote_ids.get(head, head)

    if remote_head is None:
        api(
            "POST",
            f"{repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": new_head},
        )
    else:
        api(
            "PATCH",
            f"{repo}/git/refs/heads/{quoted}",
            {"sha": new_head, "force": force},
        )
    if new_head == head:
        git("update-ref", f"refs/remotes/{remote}/{branch}", head)
        print(f"{branch} -> {head[:12]}")
    else:
        print(
            f"{branch} -> {new_head[:12]}. GitHub's commit ids differ from the "
            "local ones, so fetch the branch before building on it.",
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PushError as error:
        print(f"github_push: {error}", file=sys.stderr)
        sys.exit(1)
