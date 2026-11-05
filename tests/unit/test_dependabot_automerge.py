"""Tests for the review-gated Dependabot automerge lane.

Snapshots are built in the shape `collect()` produces, so every case runs the
same `evaluate()` the scheduled workflow does, with no GitHub access.
"""

import copy
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dependabot_automerge.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dependabot_automerge", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ast-grep-ignore: no-dict-any - snapshots mirror trimmed GitHub REST payloads
Snapshot = dict[str, Any]

lane = _script()
POLICY: dict[str, object] = dict(
    json.loads(
        (ROOT / ".github/dependabot-automerge-policy.json").read_text(encoding="utf-8")
    ),
    mode="report-only",
)
HEAD = "a" * 40
COMMITTED = "2026-10-10T00:00:00Z"
REVIEWER = {"id": POLICY["reviewer_id"], "login": "chatgpt-codex-connector[bot]"}
RULES = [{"type": "pull_request", "parameters": {}}]
PYPROJECT_PATCH = (
    '@@ -1,3 +1,3 @@\n dependencies = [\n-    "httpx>=0.27.0",\n+    "httpx>=0.28.1",\n'
)


def _uv_lock(**versions: str) -> str:
    return 'version = 1\nrevision = 3\nrequires-python = "==3.13.*"\n\n' + "".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
        f'source = {{ registry = "https://pypi.org/simple" }}\n\n'
        for name, version in versions.items()
    )


def _npm_lock(**versions: str) -> str:
    packages: dict[str, object] = {"": {"name": "frontend", "version": "0.0.0"}}
    for name, version in versions.items():
        packages[f"node_modules/{name}"] = {"version": version}
    return json.dumps({"name": "frontend", "lockfileVersion": 3, "packages": packages})


def _file(path: str, **extra: str) -> dict[str, str]:
    return {
        "filename": path,
        "status": "modified",
        "head_type": "file",
        "base_type": "file",
        **extra,
    }


def _uv_lock_file(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    # GitHub omits the patch for large diffs, which uv.lock usually is.
    return _file("uv.lock", base_text=_uv_lock(**before), head_text=_uv_lock(**after))


def candidate(*files: dict[str, str]) -> Snapshot:
    if not files:
        files = (
            _file("pyproject.toml", patch=PYPROJECT_PATCH),
            _uv_lock_file({"httpx": "0.27.0"}, {"httpx": "0.28.1"}),
        )
    repo = {"full_name": POLICY["repository"]}
    workflow = POLICY["checks"]["all-tests"]  # type: ignore[index]
    return {
        "pr": {
            "number": 1,
            "state": "open",
            "draft": False,
            "changed_files": len(files),
            "commits": 1,
            "mergeable": True,
            "labels": [],
            "user": {"id": POLICY["creator_id"]},
            "created_at": "2026-10-09T00:00:00Z",
            "head": {"sha": HEAD, "ref": "dependabot/uv/httpx-0.28.1", "repo": repo},
            "base": {"sha": "b" * 40, "ref": "main", "repo": repo},
        },
        "observed_at": "2026-10-10T02:00:00Z",
        "errors": [],
        "rules": copy.deepcopy(RULES),
        "commits": [
            {
                "sha": HEAD,
                "committer": {"login": "web-flow"},
                "commit": {
                    "author": {"email": POLICY["commit_email"]},
                    "committer": {"date": COMMITTED},
                    "verification": {"verified": True, "reason": "valid"},
                },
            }
        ],
        "files": [copy.deepcopy(f) for f in files],
        "reviews": [],
        "review_comments": [],
        "reactions": [
            {
                "id": 9,
                "user": REVIEWER,
                "content": "+1",
                "created_at": "2026-10-10T00:40:00Z",
            }
        ],
        "comments": [
            {
                "id": 5,
                "user": REVIEWER,
                "created_at": "2026-10-10T00:35:00Z",
                "body": lane.SUMMARY_MARKER
                + "\n| 📝 **Code Review** | ✅ **Completed** | `aaaaaaa` | Manual |",
            }
        ],
        "runs": [
            {
                "id": 7,
                "run_number": 1,
                "run_attempt": 1,
                "path": workflow,
                "head_sha": HEAD,
                "event": "pull_request",
                "repository": repo,
                "pull_requests": [{"number": 1}],
                "status": "completed",
                "conclusion": "success",
            }
        ],
        "statuses": [],
        "jobs": {
            "7": [
                {
                    "name": "all-tests",
                    "run_id": 7,
                    "run_attempt": 1,
                    "head_sha": HEAD,
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
    }


def held(snapshot: Snapshot, reason: str) -> Snapshot:
    result = lane.evaluate(snapshot, POLICY)
    assert result["decision"] == "hold", result
    assert reason in result["reason"], result
    return result


def test_routine_grouped_update_is_eligible() -> None:
    result = lane.evaluate(candidate(), POLICY)
    assert result["decision"] == "eligible", result
    assert result["status"] == "queued"


def test_lockfile_only_update_is_eligible() -> None:
    """Dependabot's uv updates usually leave an already satisfied floor alone."""
    snap = candidate(
        _uv_lock_file(
            {"ruff": "0.16.4", "pylint": "4.0.7"}, {"ruff": "0.16.8", "pylint": "4.0.9"}
        )
    )
    assert lane.evaluate(snap, POLICY)["decision"] == "eligible"


def test_lockfile_major_change_is_manual() -> None:
    snap = candidate(_uv_lock_file({"pillow": "11.3.0"}, {"pillow": "12.0.0"}))
    assert held(snap, "pillow 11.3.0 -> 12.0.0")["status"] == "manual"


def test_new_or_dropped_transitive_package_is_not_a_major() -> None:
    snap = candidate(
        _uv_lock_file(
            {"httpx": "0.27.0", "sniffio": "1.3.1"},
            {"httpx": "0.28.1", "h11": "0.16.0"},
        )
    )
    assert lane.evaluate(snap, POLICY)["decision"] == "eligible"


def test_every_fork_of_a_uv_package_is_checked() -> None:
    """A forked resolution locks one package at several versions."""
    file = _uv_lock_file({"httpx": "0.27.0"}, {"httpx": "0.28.1"})
    fork = '[[package]]\nname = "example"\nversion = "{}"\n\n'
    file["base_text"] += fork.format("1.9.0") + fork.format("3.0.0")
    file["head_text"] += fork.format("2.0.0") + fork.format("3.0.0")
    held(candidate(file), "example 1.9.0, 3.0.0 -> 2.0.0, 3.0.0")


def test_nested_npm_copy_is_checked() -> None:
    def lock(nested: str) -> str:
        return json.dumps({
            "packages": {
                "": {},
                "node_modules/semver": {"version": "7.6.0"},
                "node_modules/a/node_modules/semver": {"version": nested},
            }
        })

    file = _file(
        "frontend/package-lock.json", base_text=lock("5.7.2"), head_text=lock("6.3.1")
    )
    held(candidate(file), "semver 5.7.2, 7.6.0 -> 6.3.1, 7.6.0")


def test_npm_lockfile_major_change_is_manual() -> None:
    snap = candidate(
        _file(
            "frontend/package-lock.json",
            base_text=_npm_lock(vite="7.1.2"),
            head_text=_npm_lock(vite="8.0.0"),
        )
    )
    held(snap, "vite 7.1.2 -> 8.0.0")


def test_denied_package_in_lockfile_is_manual() -> None:
    snap = candidate(_uv_lock_file({"sqlalchemy": "2.0.40"}, {"sqlalchemy": "2.0.44"}))
    assert held(snap, "denied package sqlalchemy")["status"] == "manual"


def test_denied_package_only_named_by_a_neighbour_is_not_held() -> None:
    """A lockfile is judged by what moved, not by which names its diff mentions."""
    snap = candidate(
        _uv_lock_file(
            {"sqlalchemy": "2.0.40", "greenlet": "3.2.3"},
            {"sqlalchemy": "2.0.40", "greenlet": "3.2.4"},
        )
    )
    assert lane.evaluate(snap, POLICY)["decision"] == "eligible"


def test_unparseable_lockfile_is_held() -> None:
    snap = candidate(_file("uv.lock", base_text="[[package]", head_text=_uv_lock()))
    held(snap, "unparseable lockfile")


def test_manifest_major_change_is_manual() -> None:
    patch = '@@ -1 +1 @@\n-    "openai>=1.109.1",\n+    "openai>=2.44.0",\n'
    held(candidate(_file("pyproject.toml", patch=patch)), "1.109.1 -> 2.44.0")


def test_denied_paths_are_manual() -> None:
    patch = "@@ -1 +1 @@\n-uses: actions/checkout@v6\n+uses: actions/checkout@v7\n"
    held(candidate(_file(".github/workflows/ci.yml", patch=patch)), "denied path")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("state", "closed", "not an admitted"),
        ("draft", True, "not an admitted"),
        ("user", {"id": 1}, "not an admitted"),
        ("labels", [{"name": "stop-updating"}], "stop-updating hold"),
    ],
)
def test_admission(field: str, value: object, reason: str) -> None:
    snap = candidate()
    snap["pr"][field] = value
    assert held(snap, reason)["status"] == "manual"


def test_branch_outside_dependabot_namespace_is_not_admitted() -> None:
    snap = candidate()
    snap["pr"]["head"]["ref"] = "claude/fix"
    held(snap, "not an admitted")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("committer", "login"), "werdnum"),
        (("commit", "verification", "verified"), False),
        (("commit", "author", "email"), "someone@example.com"),
    ],
)
def test_commit_must_be_verified_dependabot_commit(
    path: tuple[str, ...], value: object
) -> None:
    snap = candidate()
    target = snap["commits"][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    held(snap, "verified Dependabot platform commit")


def test_a_pushed_fix_makes_the_pr_manual() -> None:
    snap = candidate()
    snap["pr"]["commits"] = 2
    held(snap, "exactly one commit")


def test_codex_findings_are_manual() -> None:
    snap = candidate()
    snap["reviews"] = [{"id": 1, "user": REVIEWER, "state": "COMMENTED"}]
    assert held(snap, "manual-review-findings")["status"] == "manual"


def test_review_older_than_head_is_stale_and_nudged() -> None:
    snap = candidate()
    snap["reactions"][0]["created_at"] = "2026-10-09T23:00:00Z"
    result = held(snap, "review-stale")
    assert result["nudge"] is True
    assert result["status"] == "waiting"


def test_unanswered_review_request_blocks() -> None:
    snap = candidate()
    snap["reactions"] = []
    snap["comments"].append({
        "id": 6,
        "user": {"id": 1, "login": "agent[bot]"},
        "body": "@codex review",
        "created_at": "2026-10-10T00:31:00Z",
    })
    snap["observed_at"] = "2026-10-11T01:00:00Z"
    assert held(snap, "review-unanswered")["status"] == "blocked"


def test_eyes_left_behind_by_a_finished_review_is_ignored() -> None:
    snap = candidate()
    snap["reactions"].append({
        "id": 8,
        "user": REVIEWER,
        "content": "eyes",
        "created_at": "2026-10-10T00:36:00Z",
    })
    assert lane.evaluate(snap, POLICY)["decision"] == "eligible"


def test_eyes_newer_than_the_clean_review_is_running() -> None:
    snap = candidate()
    snap["reactions"].append({
        "id": 10,
        "user": REVIEWER,
        "content": "eyes",
        "created_at": "2026-10-10T00:41:00Z",
    })
    assert held(snap, "review-running")["status"] == "waiting"


def test_newer_summary_supersedes_one_for_an_earlier_head() -> None:
    snap = candidate()
    old = copy.deepcopy(snap["comments"][0])
    old["id"] = 4
    old["created_at"] = "2026-10-09T12:00:00Z"
    old["body"] = old["body"].replace("aaaaaaa", "ccccccc")
    snap["comments"].insert(0, old)
    assert lane.evaluate(snap, POLICY)["decision"] == "eligible"


def test_summary_naming_another_commit_waits() -> None:
    snap = candidate()
    snap["comments"][0]["body"] = snap["comments"][0]["body"].replace(
        "aaaaaaa", "ccccccc"
    )
    held(snap, "review-summary-mismatch")


def test_required_check_comes_from_policy_without_a_ruleset() -> None:
    snap = candidate()
    snap["jobs"]["7"][0]["conclusion"] = "failure"
    assert held(snap, "ci-failure: all-tests")["status"] == "blocked"


def test_missing_ci_run_waits() -> None:
    snap = candidate()
    snap["runs"] = []
    assert held(snap, "ci-missing-trusted-run")["status"] == "waiting"


def test_unknown_ruleset_rule_holds() -> None:
    snap = candidate()
    snap["rules"].append({"type": "merge_queue", "parameters": {}})
    held(snap, "ci-unknown-rule: merge_queue")


def test_unmapped_ruleset_check_holds() -> None:
    snap = candidate()
    snap["rules"].append({
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "lint"}]},
    })
    held(snap, "ci-unmapped-required-check: lint")


def _alembic_heads(*reports: tuple[str, str, str]) -> Snapshot:
    snap = candidate()
    snap["rules"].append({
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "alembic-heads"}]},
    })
    snap["statuses"] = [
        {
            "id": index,
            "context": "alembic-heads",
            "state": state,
            "created_at": created_at,
            "creator": {"login": login},
        }
        for index, (state, login, created_at) in enumerate(reports)
    ]
    return snap


def test_required_commit_status_passes() -> None:
    snap = _alembic_heads(
        ("pending", "github-actions[bot]", "2026-10-10T00:10:00Z"),
        ("success", "github-actions[bot]", "2026-10-10T00:11:00Z"),
    )
    assert lane.evaluate(snap, POLICY)["decision"] == "eligible"


def test_missing_or_pending_commit_status_waits() -> None:
    assert held(_alembic_heads(), "ci-pending: alembic-heads")["status"] == "waiting"
    snap = _alembic_heads(("pending", "github-actions[bot]", "2026-10-10T00:10:00Z"))
    assert held(snap, "ci-pending: alembic-heads")["status"] == "waiting"


def test_failed_commit_status_blocks() -> None:
    snap = _alembic_heads(
        ("success", "github-actions[bot]", "2026-10-10T00:10:00Z"),
        ("failure", "github-actions[bot]", "2026-10-10T00:20:00Z"),
    )
    assert held(snap, "ci-failure: alembic-heads")["status"] == "blocked"


def test_commit_status_from_another_poster_holds() -> None:
    """Anyone with status write can post a context; only the workflow's report counts."""
    snap = _alembic_heads(
        ("failure", "github-actions[bot]", "2026-10-10T00:10:00Z"),
        ("success", "someone", "2026-10-10T00:20:00Z"),
    )
    held(snap, "ci-untrusted-status: alembic-heads")


def test_alembic_plugin_is_not_the_denied_package() -> None:
    snap = candidate(
        _uv_lock_file({"pytest-alembic": "0.12.1"}, {"pytest-alembic": "0.12.2"})
    )
    assert lane.evaluate(snap, POLICY)["decision"] == "eligible"
    snap = candidate(_uv_lock_file({"alembic": "1.16.4"}, {"alembic": "1.16.5"}))
    held(snap, "denied package alembic")


def test_collection_errors_have_no_status() -> None:
    snap = candidate()
    snap["errors"] = ["GitHub read failed: repos/x/pulls/1"]
    assert held(snap, "GitHub read failed")["status"] is None


def test_only_oldest_eligible_pr_is_chosen_within_the_window() -> None:
    first = lane.evaluate(candidate(), POLICY)
    later = dict(first, pr=2, created_at="2026-10-09T05:00:00Z")
    clear = {"state": "clear", "reasons": [], "last_auto_merge": None}
    midday = dt.datetime(2026, 10, 10, 2, tzinfo=dt.UTC)  # 13:00 in Sydney
    chosen = lane.choose([later, first], dict(clear, reasons=[]), POLICY, midday)
    assert chosen is not None and chosen["pr"] == 1
    assert chosen["decision"] == "would-merge"
    night = dt.datetime(2026, 10, 10, 14, tzinfo=dt.UTC)  # 01:00 in Sydney
    assert (
        lane.choose(
            [lane.evaluate(candidate(), POLICY)], dict(clear, reasons=[]), POLICY, night
        )
        is None
    )


# --- Agent PRs ------------------------------------------------------------------
#
# A coding agent's dependency fix opens as the owner from a claude/ branch.
# Nothing about its author is trusted, so it qualifies only on what its diff
# shows: version-only manifest lines, and lockfile entries that moved to
# another release from the public registry.

AGENT: Snapshot = POLICY["agent"]  # type: ignore[assignment]
NOON = dt.datetime(2026, 10, 10, 2, tzinfo=dt.UTC)  # 13:00 in Sydney


def _uv_entry(name: str, version: str, **overrides: object) -> dict[str, object]:
    stem = name.replace("-", "_")
    entry: dict[str, object] = {
        "name": name,
        "version": version,
        "source": {"registry": AGENT["uv_registry"]},
        "sdist": {
            "url": f"{AGENT['uv_file_host']}packages/aa/{stem}-{version}.tar.gz",
            "hash": "sha256:" + "1" * 64,
        },
        "wheels": [
            {
                "url": f"{AGENT['uv_file_host']}packages/bb/{stem}-{version}-py3-none-any.whl",
                "hash": "sha256:" + "2" * 64,
            }
        ],
    }
    entry.update(overrides)
    return entry


def _toml_value(value: object) -> str:
    if isinstance(value, dict):
        return (
            "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(value)


def _uv_lock_entries(*entries: dict[str, object]) -> str:
    project = {
        "name": "family-assistant",
        "version": "0.1.0",
        "source": {"editable": "."},
    }
    return 'version = 1\nrevision = 3\nrequires-python = "==3.14.*"\n\n' + "".join(
        "[[package]]\n"
        + "".join(f"{key} = {_toml_value(value)}\n" for key, value in entry.items())
        + "\n"
        for entry in (project, *entries)
    )


def agent_candidate(*files: dict[str, str]) -> Snapshot:
    if not files:
        files = (
            _file(
                "uv.lock",
                base_text=_uv_lock_entries(_uv_entry("pillow", "11.3.0")),
                head_text=_uv_lock_entries(_uv_entry("pillow", "11.3.1")),
            ),
        )
    snap = candidate(*files)
    snap["pr"]["user"] = {"id": AGENT["creator_id"]}
    snap["pr"]["head"]["ref"] = "claude/project-thread-abc123"
    snap["pr"]["commits"] = 2
    signed = {"verified": True, "reason": "valid"}
    snap["commits"] = [
        {
            "sha": "c" * 40,
            "committer": {"login": "claude"},
            "commit": {
                "author": {"email": "noreply@anthropic.com"},
                "committer": {"date": "2026-10-09T22:00:00Z"},
                "verification": dict(signed),
            },
        },
        {
            "sha": HEAD,
            "committer": {"login": "claude"},
            "commit": {
                "author": {"email": "noreply@anthropic.com"},
                "committer": {"date": COMMITTED},
                "verification": dict(signed),
            },
        },
    ]
    return snap


def test_agent_lockfile_fix_is_eligible_but_only_reported() -> None:
    result = lane.evaluate(agent_candidate(), POLICY)
    assert result["kind"] == "agent"
    assert result["decision"] == "eligible", result
    merging = dict(POLICY, mode="merge")
    assert (
        lane.choose([result], {"state": "clear", "reasons": []}, merging, NOON) is None
    )
    assert result["decision"] == "would-merge (agent lane is report-only)"
    assert not lane.acting("agent", merging)
    assert lane.acting("dependabot", merging)


def test_report_only_agent_pr_does_not_take_the_merge_slot() -> None:
    agent = lane.evaluate(agent_candidate(), POLICY)
    agent["created_at"] = "2026-10-01T00:00:00Z"
    dependabot = lane.evaluate(candidate(), POLICY)
    merging = dict(POLICY, mode="merge")
    chosen = lane.choose(
        [agent, dependabot], {"state": "clear", "reasons": []}, merging, NOON
    )
    assert chosen is dependabot


def test_agent_manifest_change_must_be_version_only() -> None:
    ok = '@@ -1 +1 @@\n-    "pillow>=11.3.0",\n+    "pillow>=11.3.1",\n'
    snap = agent_candidate(_file("pyproject.toml", patch=ok))
    assert lane.evaluate(snap, POLICY)["decision"] == "eligible"
    renamed = '@@ -1 +1 @@\n-    "pillow>=11.3.0",\n+    "pilow>=11.3.0",\n'
    held(agent_candidate(_file("pyproject.toml", patch=renamed)), "more than a version")


def test_agent_pr_outside_dependency_files_is_manual() -> None:
    patch = "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    snap = agent_candidate(_file("src/family_assistant/config.py", patch=patch))
    assert held(snap, "outside dependency files")["status"] == "manual"


def test_agent_may_not_add_a_package() -> None:
    snap = agent_candidate(
        _file(
            "uv.lock",
            base_text=_uv_lock_entries(_uv_entry("authlib", "1.6.4")),
            head_text=_uv_lock_entries(
                _uv_entry("authlib", "1.9.4"), _uv_entry("joserfc", "1.7.7")
            ),
        )
    )
    held(snap, "added package joserfc")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"source": {"registry": "https://evil.example/simple"}},
            "not from the registry",
        ),
        (
            {
                "sdist": {
                    "url": "https://evil.example/pillow-11.3.1.tar.gz",
                    "hash": "sha256:1",
                }
            },
            "not from the registry",
        ),
        (
            {
                "sdist": {
                    "url": "https://files.pythonhosted.org/packages/aa/other-11.3.1.tar.gz",
                    "hash": "sha256:1",
                }
            },
            "not from the registry",
        ),
    ],
)
def test_agent_lock_entry_must_come_from_the_registry(
    overrides: dict[str, object], reason: str
) -> None:
    snap = agent_candidate(
        _file(
            "uv.lock",
            base_text=_uv_lock_entries(_uv_entry("pillow", "11.3.0")),
            head_text=_uv_lock_entries(_uv_entry("pillow", "11.3.1", **overrides)),
        )
    )
    held(snap, reason)


def test_agent_may_not_repoint_a_package_at_the_same_version() -> None:
    sneaky = _uv_entry(
        "pillow",
        "11.3.0",
        sdist={
            "url": "https://files.pythonhosted.org/packages/zz/pillow-11.3.0.tar.gz",
            "hash": "sha256:" + "9" * 64,
        },
    )
    snap = agent_candidate(
        _file(
            "uv.lock",
            base_text=_uv_lock_entries(_uv_entry("pillow", "11.3.0")),
            head_text=_uv_lock_entries(sneaky),
        )
    )
    held(snap, "without changing its version")


def test_agent_may_not_change_the_lockfile_header() -> None:
    base = _uv_lock_entries(_uv_entry("pillow", "11.3.0"))
    head = _uv_lock_entries(_uv_entry("pillow", "11.3.1")).replace(
        "revision = 3", "revision = 4"
    )
    held(agent_candidate(_file("uv.lock", base_text=base, head_text=head)), "header")


def test_agent_npm_entry_must_resolve_from_the_registry() -> None:
    def lock(version: str, resolved: str | None = None) -> str:
        url = resolved or f"{AGENT['npm_registry']}vite/-/vite-{version}.tgz"
        return json.dumps({
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "frontend"},
                "node_modules/vite": {
                    "version": version,
                    "resolved": url,
                    "integrity": "sha512-abc",
                },
            },
        })

    good = _file(
        "frontend/package-lock.json", base_text=lock("8.3.2"), head_text=lock("8.3.5")
    )
    assert lane.evaluate(agent_candidate(good), POLICY)["decision"] == "eligible"
    bad = _file(
        "frontend/package-lock.json",
        base_text=lock("8.3.2"),
        head_text=lock("8.3.5", "https://evil.example/vite-8.3.5.tgz"),
    )
    held(agent_candidate(bad), "not from the registry")


def test_agent_commits_must_all_be_signed() -> None:
    snap = agent_candidate()
    snap["commits"][0]["commit"]["verification"]["verified"] = False
    held(snap, "unsigned commit")


def test_agent_review_is_judged_against_the_head_commit() -> None:
    snap = agent_candidate()
    snap["reactions"][0]["created_at"] = "2026-10-09T23:00:00Z"  # before the head
    held(snap, "review-stale")
