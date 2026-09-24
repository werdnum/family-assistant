"""The sandbox's API-based git push, against a GitHub fake backed by real git.

The fake implements the Git Data API endpoints the helper calls by writing the
same objects into a bare repository with git's own plumbing, so "the push
worked" is checked the strongest way available: the bare repository's branch
ends up at the local commit id, which only happens when every tree and every
commit header was recreated byte for byte.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from family_assistant.llm import sandbox_helpers

if TYPE_CHECKING:
    from collections.abc import Iterator

HELPER = Path(sandbox_helpers.__file__).parent / "github_push.py"
_REPO_PREFIX = "/repos/werdnum/example"
_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Agent",
    "GIT_AUTHOR_EMAIL": "agent@example.com",
    "GIT_COMMITTER_NAME": "Agent",
    "GIT_COMMITTER_EMAIL": "agent@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(
    cwd: Path, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None
) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=True,
        env={**os.environ, **_IDENTITY_ENV, **(env or {})},
    )
    return result.stdout.decode().strip()


class _FakeGitHub(BaseHTTPRequestHandler):
    """The Git Data API over a bare repository, via git plumbing."""

    bare: Path
    requests_seen: list[tuple[str, str]]
    # Stands in for any way GitHub might serialize a commit differently from
    # git, which would give the pushed commit a different id.
    alter_messages: bool = False

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _reply(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _route(self, method: str) -> None:
        self.requests_seen.append((method, self.path))
        if "Authorization" in self.headers:
            self._reply(400, {"message": "the helper must not send credentials"})
            return
        if not _git(self.bare, "for-each-ref"):
            self._reply(409, {"message": "Git Repository is empty."})
            return
        path = self.path.removeprefix(_REPO_PREFIX)
        handler = {
            ("GET", "/git/ref/heads/"): self._get_ref,
            ("POST", "/git/blobs"): self._create_blob,
            ("POST", "/git/trees"): self._create_tree,
            ("POST", "/git/commits"): self._create_commit,
            ("POST", "/git/refs"): self._create_ref,
            ("PATCH", "/git/refs/heads/"): self._update_ref,
        }
        for (verb, prefix), action in handler.items():
            if verb == method and path.startswith(prefix):
                action(urllib.parse.unquote(path.removeprefix(prefix)))
                return
        self._reply(404, {"message": "Not Found"})

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PATCH(self) -> None:
        self._route("PATCH")

    def _get_ref(self, branch: str) -> None:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.bare,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            self._reply(404, {"message": "Not Found"})
            return
        self._reply(200, {"object": {"sha": result.stdout.decode().strip()}})

    def _create_blob(self, _: str) -> None:
        body = self._body()
        content = base64.b64decode(str(body["content"]))
        sha = _git(self.bare, "hash-object", "-w", "--stdin", stdin=content)
        self._reply(201, {"sha": sha})

    def _create_tree(self, _: str) -> None:
        body = self._body()
        with tempfile.TemporaryDirectory() as scratch:
            env = {
                "GIT_INDEX_FILE": str(Path(scratch) / "index"),
                "GIT_WORK_TREE": scratch,
            }
            if body.get("base_tree"):
                _git(self.bare, "read-tree", str(body["base_tree"]), env=env)
            entries = body["tree"]
            assert isinstance(entries, list)
            for entry in entries:
                if entry["sha"] is None:
                    _git(
                        self.bare,
                        "update-index",
                        "--force-remove",
                        entry["path"],
                        env=env,
                    )
                else:
                    _git(
                        self.bare,
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"{entry['mode']},{entry['sha']},{entry['path']}",
                        env=env,
                    )
            sha = _git(self.bare, "write-tree", env=env)
        self._reply(201, {"sha": sha})

    def _create_commit(self, _: str) -> None:
        body = self._body()
        author = body["author"]
        committer = body["committer"]
        assert isinstance(author, dict)
        assert isinstance(committer, dict)
        parents = body["parents"]
        assert isinstance(parents, list)
        args = ["commit-tree", str(body["tree"])]
        for parent in parents:
            args += ["-p", parent]
        sha = _git(
            self.bare,
            *args,
            env={
                "GIT_AUTHOR_NAME": author["name"],
                "GIT_AUTHOR_EMAIL": author["email"],
                "GIT_AUTHOR_DATE": author["date"],
                "GIT_COMMITTER_NAME": committer["name"],
                "GIT_COMMITTER_EMAIL": committer["email"],
                "GIT_COMMITTER_DATE": committer["date"],
            },
            stdin=(
                str(body["message"]) + ("[api]\n" if self.alter_messages else "")
            ).encode(),
        )
        self._reply(201, {"sha": sha})

    def _create_ref(self, _: str) -> None:
        body = self._body()
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", str(body["ref"])],
            cwd=self.bare,
            capture_output=True,
            check=False,
        )
        if exists.returncode == 0:
            self._reply(422, {"message": "Reference already exists"})
            return
        _git(self.bare, "update-ref", str(body["ref"]), str(body["sha"]))
        self._reply(201, {"object": {"sha": body["sha"]}})

    def _update_ref(self, branch: str) -> None:
        body = self._body()
        current = _git(self.bare, "rev-parse", f"refs/heads/{branch}")
        is_ancestor = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", current, str(body["sha"])],
                cwd=self.bare,
                check=False,
            ).returncode
            == 0
        )
        if not is_ancestor and not body.get("force"):
            self._reply(422, {"message": "Update is not a fast forward"})
            return
        _git(self.bare, "update-ref", f"refs/heads/{branch}", str(body["sha"]))
        self._reply(200, {"object": {"sha": body["sha"]}})


@pytest.fixture(name="github")
def github_fixture(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[Path, str, list[tuple[str, str]]]]:
    """A bare 'GitHub' repository with one commit on main, served over HTTP."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    (seed / "README.md").write_text("hello\n")
    (seed / "old.txt").write_text("to be deleted\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "Initial commit")
    _git(seed, "push", "origin", "main")

    handler = type(
        "Handler",
        (_FakeGitHub,),
        {
            "bare": bare,
            "requests_seen": [],
            "alter_messages": getattr(request, "param", False),
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield bare, f"http://127.0.0.1:{server.server_port}", handler.requests_seen
    finally:
        server.shutdown()
        server.server_close()


def _clone(tmp_path: Path, bare: Path) -> Path:
    """A working clone whose origin looks like GitHub but was fetched locally."""
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "remote", "set-url", "origin", "https://github.com/werdnum/example.git")
    return work


def _push(work: Path, api_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_IDENTITY_ENV, "FA_GITHUB_API_URL": api_url},
    )


def test_a_new_branch_arrives_with_the_local_commit_ids(
    github: tuple[Path, str, list[tuple[str, str]]], tmp_path: Path
) -> None:
    """Adds, edits, deletes, nested paths, an executable bit, a symlink and a
    merge all survive, identified by the same commit ids as locally."""
    bare, api_url, _ = github
    work = _clone(tmp_path, bare)
    _git(work, "checkout", "-b", "feature")
    (work / "src" / "pkg").mkdir(parents=True)
    (work / "src" / "pkg" / "module.py").write_text("print('hi')\n")
    script = work / "run.sh"
    script.write_text("#!/bin/sh\necho run\n")
    script.chmod(0o755)
    (work / "link").symlink_to("README.md")
    (work / "old.txt").unlink()
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "Add code\n\nWith a body.")
    _git(work, "checkout", "-b", "side", "main")
    (work / "side.txt").write_text("from the side\n")
    _git(work, "add", "side.txt")
    _git(
        work,
        "commit",
        "-m",
        "Side work",
        env={"GIT_AUTHOR_DATE": "2026-09-24T09:00:00+10:00"},
    )
    _git(work, "checkout", "feature")
    _git(work, "merge", "--no-ff", "-m", "Merge side", "side")
    head = _git(work, "rev-parse", "HEAD")

    result = _push(work, api_url, "feature")

    assert result.returncode == 0, result.stderr
    assert _git(bare, "rev-parse", "refs/heads/feature") == head


@pytest.mark.parametrize("github", [True], indirect=True)
def test_commits_github_renumbers_are_chained_onto_its_ids(
    github: tuple[Path, str, list[tuple[str, str]]], tmp_path: Path
) -> None:
    """If GitHub's copy of a commit gets a different id, its child must name
    GitHub's copy as parent, or the branch would point at a broken history."""
    bare, api_url, _ = github
    work = _clone(tmp_path, bare)
    _git(work, "checkout", "-b", "feature")
    for name in ("a", "b"):
        (work / f"{name}.txt").write_text(f"{name}\n")
        _git(work, "add", f"{name}.txt")
        _git(work, "commit", "-m", f"Add {name}")

    result = _push(work, api_url, "feature")

    assert result.returncode == 0, result.stderr
    assert "fetch the branch" in result.stdout
    pushed = _git(bare, "rev-parse", "refs/heads/feature")
    assert _git(bare, "rev-parse", f"{pushed}^{{tree}}") == _git(
        work, "rev-parse", "HEAD^{tree}"
    )
    assert _git(bare, "rev-parse", f"{pushed}~1^{{tree}}") == _git(
        work, "rev-parse", "HEAD~1^{tree}"
    )
    assert _git(bare, "rev-parse", f"{pushed}~2") == _git(
        work, "rev-parse", "origin/main"
    )


def test_a_follow_up_push_sends_only_the_new_commit(
    github: tuple[Path, str, list[tuple[str, str]]], tmp_path: Path
) -> None:
    bare, api_url, requests_seen = github
    work = _clone(tmp_path, bare)
    _git(work, "checkout", "-b", "feature")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "First")
    assert _push(work, api_url, "feature").returncode == 0
    (work / "b.txt").write_text("b\n")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-m", "Second")
    requests_seen.clear()

    result = _push(work, api_url, "feature")

    assert result.returncode == 0, result.stderr
    assert [r for r in requests_seen if r[1].endswith("/git/commits")] == [
        ("POST", f"{_REPO_PREFIX}/git/commits")
    ]
    assert _git(bare, "rev-parse", "refs/heads/feature") == _git(
        work, "rev-parse", "HEAD"
    )


def test_a_branch_name_with_url_characters_reaches_its_own_ref(
    github: tuple[Path, str, list[tuple[str, str]]], tmp_path: Path
) -> None:
    bare, api_url, _ = github
    work = _clone(tmp_path, bare)
    _git(work, "checkout", "-b", "fix#12%3")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "Fix")
    assert _push(work, api_url, "fix#12%3").returncode == 0
    (work / "b.txt").write_text("b\n")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-m", "More")

    result = _push(work, api_url, "fix#12%3")

    assert result.returncode == 0, result.stderr
    assert _git(bare, "rev-parse", "refs/heads/fix#12%3") == _git(
        work, "rev-parse", "HEAD"
    )


def test_an_empty_repository_gets_its_first_push_from_git(
    github: tuple[Path, str, list[tuple[str, str]]], tmp_path: Path
) -> None:
    """GitHub's API cannot write to a repository with no commits, so the first
    push goes through git, while its submit-time credential is still valid."""
    bare, api_url, _ = github
    work = _clone(tmp_path, bare)
    _git(bare, "update-ref", "-d", "refs/heads/main")
    _git(work, "remote", "set-url", "--push", "origin", str(bare))

    result = _push(work, api_url, "main")

    assert result.returncode == 0, result.stderr
    assert _git(bare, "rev-parse", "refs/heads/main") == _git(work, "rev-parse", "HEAD")


def test_a_git_lfs_file_is_refused_rather_than_pushed_as_a_bare_pointer(
    github: tuple[Path, str, list[tuple[str, str]]], tmp_path: Path
) -> None:
    """LFS content goes to GitHub's LFS server, which the API cannot reach;
    pushing only the pointer would leave a file nobody can check out."""
    bare, api_url, _ = github
    work = _clone(tmp_path, bare)
    (work / "model.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "0" * 64 + "\nsize 12\n"
    )
    _git(work, "add", "model.bin")
    _git(work, "commit", "-m", "Add model")
    remote_head = _git(bare, "rev-parse", "refs/heads/main")

    result = _push(work, api_url, "main")

    assert result.returncode == 1
    assert "LFS" in result.stderr
    assert _git(bare, "rev-parse", "refs/heads/main") == remote_head


def test_a_diverged_branch_is_refused_without_force(
    github: tuple[Path, str, list[tuple[str, str]]], tmp_path: Path
) -> None:
    """Pushing over someone else's work needs the same explicit ask git needs."""
    bare, api_url, _ = github
    work = _clone(tmp_path, bare)
    (work / "local.txt").write_text("local\n")
    _git(work, "add", "local.txt")
    _git(work, "commit", "-m", "Local")
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(bare), str(other))
    (other / "remote.txt").write_text("remote\n")
    _git(other, "add", "remote.txt")
    _git(other, "commit", "-m", "Remote")
    _git(other, "push", "origin", "main")
    remote_head = _git(bare, "rev-parse", "refs/heads/main")

    result = _push(work, api_url, "main")

    assert result.returncode == 1
    assert "--force" in result.stderr
    assert _git(bare, "rev-parse", "refs/heads/main") == remote_head


def test_a_rejected_credential_names_the_egress_rule(tmp_path: Path) -> None:
    """A 401 means the proxy attached nothing, which is a config problem."""

    class _Unauthorized(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"message": "Requires authentication"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Unauthorized)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    work = tmp_path / "work"
    _git(tmp_path, "init", "-b", "main", str(work))
    _git(work, "remote", "add", "origin", "https://github.com/werdnum/example.git")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "A")
    try:
        result = _push(work, f"http://127.0.0.1:{server.server_port}", "main")
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 1
    assert "api.github.com" in result.stderr
