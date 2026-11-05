#!/usr/bin/env python3
"""Review-gated Dependabot lane: evaluate, report, and (in merge mode) act.

See docs/design/dependabot-automerge.md. Ported from kube-config's Renovate
lane (.github/scripts/renovate_report.py there); the evidence rules are the
same apart from admission, provenance and lockfiles.

Evidence is read with GET requests only; candidate files are read as data and
never executed. Unknown or unavailable evidence produces a hold. Each narrow
write action is run by its own workflow job with its own token:

- --nudge: post one `@codex review` per head commit (agent App token);
- --label: show each PR's status as one `automerge: ...` label (workflow token);
- --final: re-evaluate one PR from scratch and merge it with the expected
  head SHA (reads with GH_TOKEN, merges with MERGE_TOKEN).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

# ast-grep-ignore: no-dict-any - GitHub REST payloads, trimmed but otherwise untyped
Json = dict[str, Any]


class EvidenceError(Exception):
    """Incomplete, unsupported or disqualifying evidence."""


def api(
    endpoint: str, paginated: bool = False, key: str | None = None, raw: bool = False
) -> Any:  # noqa: ANN401 - decoded GitHub JSON
    command = ["gh", "api", "--method", "GET", endpoint]
    if paginated:
        command += ["--paginate", "--slurp"]
    if raw:
        command += ["-H", "Accept: application/vnd.github.raw"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=120
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceError("GitHub read timed out") from error
    if result.returncode:
        raise EvidenceError("GitHub read failed: " + endpoint.split("?", maxsplit=1)[0])
    if raw:
        return result.stdout
    value = json.loads(result.stdout)
    if not paginated:
        return value
    return [item for page in value for item in (page[key] if key else page)]


def write(
    method: str, endpoint: str, fields: dict[str, str], token: str
) -> tuple[int, str]:
    """One write request, with an explicitly supplied token."""
    command = ["gh", "api", "--method", method, endpoint]
    for name, value in fields.items():
        command += ["-f", f"{name}={value}"]
    env = dict(os.environ, GH_TOKEN=token)
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=120, env=env
    )
    return result.returncode, result.stdout + result.stderr


def timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EvidenceError("timestamp has no timezone")
    return parsed


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def quote(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


# --- Per-PR rules -----------------------------------------------------------

SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"
SUMMARY_SHA = re.compile(r"`([0-9a-f]{7,40})`")
# A clean-review receipt comment, which Codex may post instead of the +1. It
# names the commit it reviewed.
CLEAN_RECEIPT = re.compile(
    r"\A(?:<!-- codex-review-receipt:v1 -->\s*)?Codex Review: Didn't find any major issues\."
    r".*?\*\*Reviewed commit:\*\* `([0-9a-f]{7,40})`",
    re.S,
)
FAILED_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
}
KNOWN_RULES = {
    "creation",
    "update",
    "deletion",
    "non_fast_forward",
    "pull_request",
    "required_status_checks",
    "required_linear_history",
    "required_signatures",
}
# Hash-like tokens (digests and lockfile hashes) are not versions.
HASHLIKE = re.compile(
    r"sha\d+[:-][A-Za-z0-9+/=]+|h1:[A-Za-z0-9+/=]+|[A-Za-z0-9+/=_-]{20,}"
)
VERSION = re.compile(r"\d+(?:\.\d+)*")


def kind_of(pr: Json, policy: Json) -> str | None:
    """`dependabot`, `agent` (a coding agent's dependency fix), or None."""
    ref, author = pr["head"]["ref"], pr["user"]["id"]
    if ref.startswith("dependabot/") and author == policy["creator_id"]:
        return "dependabot"
    agent = policy.get("agent")
    if (
        agent
        and ref.startswith(agent["branch_prefix"])
        and author == agent["creator_id"]
    ):
        return "agent"
    return None


def acting(kind: str | None, policy: Json) -> bool:
    """Whether the lane may act on a PR of this kind, not just report on it."""
    if policy.get("mode") != "merge":
        return False
    return kind != "agent" or policy["agent"]["mode"] == "merge"


def admission(pr: Json, policy: Json) -> None:
    if (
        pr["state"] != "open"
        or pr["draft"]
        or pr["base"]["ref"] != "main"
        or pr["base"]["repo"]["full_name"] != policy["repository"]
        or pr["head"]["repo"]["full_name"] != policy["repository"]
        or kind_of(pr, policy) is None
    ):
        raise EvidenceError("not an admitted Dependabot or agent candidate")
    if policy["stop_label"] in [label["name"] for label in pr["labels"]]:
        raise EvidenceError(policy["stop_label"] + " hold")


def agent_provenance(snapshot: Json) -> dt.datetime:
    """An agent PR may have several commits; each must carry a valid signature.

    Who wrote the commits proves nothing here (agents open PRs as the owner),
    so the gates that matter are the file and lockfile rules below.
    """
    commits = snapshot["commits"]
    if not commits or len(commits) != snapshot["pr"]["commits"]:
        raise EvidenceError("incomplete commit list")
    if commits[-1]["sha"] != snapshot["pr"]["head"]["sha"]:
        raise EvidenceError("last commit is not the head")
    for commit in commits:
        verification = commit["commit"]["verification"]
        if verification["verified"] is not True or verification["reason"] != "valid":
            raise EvidenceError("unsigned commit " + commit["sha"][:7])
    return timestamp(commits[-1]["commit"]["committer"]["date"])


def provenance(snapshot: Json, policy: Json) -> dt.datetime:
    if kind_of(snapshot["pr"], policy) == "agent":
        return agent_provenance(snapshot)
    commits = snapshot["commits"]
    if len(commits) != 1 or snapshot["pr"]["commits"] != 1:
        raise EvidenceError("not exactly one commit")
    commit = commits[0]
    if commit["sha"] != snapshot["pr"]["head"]["sha"]:
        raise EvidenceError("commit is not the head")
    details = commit["commit"]
    if (
        details["author"]["email"] != policy["commit_email"]
        or (commit.get("committer") or {}).get("login") != "web-flow"
        or details["verification"]["verified"] is not True
        or details["verification"]["reason"] != "valid"
    ):
        raise EvidenceError("commit is not a verified Dependabot platform commit")
    return timestamp(details["committer"]["date"])


def denied_path(path: str, policy: Json) -> bool:
    return any(re.search(pattern, path) for pattern in policy["deny_paths"])


def changed_pairs(patch: str) -> list[tuple[str | None, str | None]]:
    """Pair each hunk's removed lines with its added lines, in order."""
    pairs: list[tuple[str | None, str | None]] = []
    removed: list[str] = []
    added: list[str] = []
    for line in [*patch.splitlines(), " "]:
        if line.startswith("-"):
            if added:
                pairs += list(zip(removed, added, strict=False))
                removed, added = [], []
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        else:
            pairs += list(zip(removed, added, strict=False))
            if len(removed) != len(added):
                pairs.append((None, None))
            removed, added = [], []
    return pairs


def major(version: str) -> str:
    return version.split(".", maxsplit=1)[0]


def major_change(before: str, after: str) -> str | None:
    old = VERSION.findall(HASHLIKE.sub("", before))
    new = VERSION.findall(HASHLIKE.sub("", after))
    if len(old) != len(new):
        return "unpaired version tokens"
    for a, b in zip(old, new, strict=True):
        if a != b and major(a) != major(b):
            return f"{a} -> {b}"
    return None


# --- Lockfiles ----------------------------------------------------------------
#
# Dependabot's uv updates usually change only uv.lock (a floor that is already
# satisfied is left alone), and its security updates for transitive packages
# always do, so a lockfile is compared by the package versions it resolves
# rather than by its diff lines. Both sides are parsed as data.


# A lockfile can hold one package at several versions: a forked uv resolution,
# or npm's nested node_modules. Each name maps to every version it is locked at.
Versions = dict[str, frozenset[str]]


def _group(pairs: list[tuple[str, str]]) -> Versions:
    grouped: dict[str, set[str]] = {}
    for name, version in pairs:
        grouped.setdefault(name, set()).add(version)
    return {name: frozenset(versions) for name, versions in grouped.items()}


def uv_lock_versions(text: str) -> Versions:
    return _group([
        (package["name"], package["version"])
        for package in tomllib.loads(text).get("package", [])
        if "version" in package
    ])


def npm_lock_versions(text: str) -> Versions:
    return _group([
        (path.rsplit("node_modules/", 1)[-1], entry["version"])
        for path, entry in json.loads(text).get("packages", {}).items()
        if path and "version" in entry
    ])


LOCKFILES: dict[str, Callable[[str], Versions]] = {
    "uv.lock": uv_lock_versions,
    "package-lock.json": npm_lock_versions,
}


def lockfile(path: str) -> Callable[[str], Versions] | None:
    return LOCKFILES.get(path.rsplit("/", 1)[-1])


def lock_changes(
    file: Json,
) -> dict[str, tuple[frozenset[str] | None, frozenset[str] | None]]:
    """Each package whose resolved version the lockfile change moves."""
    parse = lockfile(file["filename"])
    assert parse is not None
    try:
        before, after = parse(file["base_text"]), parse(file["head_text"])
    except (
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as error:
        raise EvidenceError("unparseable lockfile: " + file["filename"]) from error
    return {
        name: (before.get(name), after.get(name))
        for name in before.keys() | after.keys()
        if before.get(name) != after.get(name)
    }


# --- Agent lockfile entries ---------------------------------------------------
#
# A lockfile written by an agent is checked entry by entry, because a version
# comparison alone would miss a package repointed at another source. An entry
# may change only by moving to another version from the public registry; no
# package may appear (the dependency list that pulls it in is unverified
# text); the root project's own entry and the file's header are exempt from
# the first rule and forbidden from changing, respectively.

Entries = dict[str, list[str]]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def uv_entries(text: str) -> tuple[str, Entries]:
    data = tomllib.loads(text)
    header = _canonical({k: v for k, v in data.items() if k != "package"})
    entries: dict[str, list[str]] = {}
    for package in data.get("package", []):
        entries.setdefault(package["name"], []).append(_canonical(package))
    return header, {name: sorted(items) for name, items in entries.items()}


def npm_entries(text: str) -> tuple[str, Entries]:
    data = json.loads(text)
    header = _canonical({k: v for k, v in data.items() if k != "packages"})
    entries: dict[str, list[str]] = {}
    for path, entry in data.get("packages", {}).items():
        name = path.rsplit("node_modules/", 1)[-1] if path else ""
        entries.setdefault(name, []).append(_canonical(dict(entry, path=path)))
    return header, {name: sorted(items) for name, items in entries.items()}


def _project_entry(entry: Json) -> bool:
    """The repository's own project, whose versions come from its manifest."""
    return ("path" in entry and not entry["path"]) or entry.get("source") in (
        {"editable": "."},
        {"virtual": "."},
    )


def _dist_token(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value).lower()


def _uv_entry_ok(entry: Json, policy: Json) -> bool:
    agent = policy["agent"]
    if entry.get("source") != {"registry": agent["uv_registry"]}:
        return False
    stem, version = _dist_token(entry["name"]), _dist_token(entry["version"])
    for artifact in filter(None, [entry.get("sdist"), *entry.get("wheels", [])]):
        url = artifact.get("url", "")
        base = _dist_token(url.rsplit("/", 1)[-1])
        if (
            not url.startswith(agent["uv_file_host"])
            or not base.startswith(f"{stem}_{version}")
            or not str(artifact.get("hash", "")).startswith("sha256:")
        ):
            return False
    return True


def _npm_entry_ok(entry: Json, policy: Json) -> bool:
    name = entry["path"].rsplit("node_modules/", 1)[-1]
    expected = (
        f"{policy['agent']['npm_registry']}{name}/-/"
        f"{name.rsplit('/', 1)[-1]}-{entry.get('version')}.tgz"
    )
    return entry.get("resolved") == expected and str(
        entry.get("integrity", "")
    ).startswith("sha512-")


def agent_lock_rules(file: Json, policy: Json) -> None:
    path = file["filename"]
    uv = path.rsplit("/", 1)[-1] == "uv.lock"
    parse = uv_entries if uv else npm_entries
    entry_ok = _uv_entry_ok if uv else _npm_entry_ok
    try:
        base_header, before = parse(file["base_text"])
        head_header, after = parse(file["head_text"])
    except (
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as error:
        raise EvidenceError("unparseable lockfile: " + path) from error
    if base_header != head_header:
        raise EvidenceError("agent changed the lockfile header in " + path)
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new or new is None:
            continue  # unchanged, or dropped
        if old is None:
            raise EvidenceError(f"agent added package {name} in {path}")
        old_versions = {json.loads(raw).get("version") for raw in old}
        for raw in set(new) - set(old):
            entry = json.loads(raw)
            if _project_entry(entry):
                continue
            if entry.get("version") in old_versions:
                raise EvidenceError(
                    f"agent changed {name} without changing its version"
                )
            if not entry_ok(entry, policy):
                raise EvidenceError(
                    f"agent lock entry for {name} is not from the registry"
                )


def denied_package(name: str, policy: Json) -> bool:
    return any(re.search(p, name, re.I) for p in policy["deny_packages"])


def file_rules(snapshot: Json, policy: Json) -> None:
    files = snapshot["files"]
    if not files or len(files) != snapshot["pr"]["changed_files"]:
        raise EvidenceError("incomplete or empty changed-file inventory")
    if len(files) > policy["max_files"]:
        raise EvidenceError("too many files")
    agent = kind_of(snapshot["pr"], policy) == "agent"
    for file in files:
        if agent and file["filename"] not in policy["agent"]["allowed_files"]:
            raise EvidenceError(
                "agent change outside dependency files: " + file["filename"]
            )
    for file in files:
        path = file["filename"]
        if file["status"] != "modified":
            raise EvidenceError("not a plain modification: " + path)
        if denied_path(path, policy):
            raise EvidenceError("denied path: " + path)
        if lockfile(path) is None and "patch" not in file:
            raise EvidenceError("no patch available: " + path)
    for file in files:
        if lockfile(file["filename"]):
            continue
        hunks = [line for line in file["patch"].splitlines() if line[:1] in "+-"]
        if any(denied_package(line, policy) for line in hunks):
            raise EvidenceError("denied package in " + file["filename"])
        for before, after in changed_pairs(file["patch"]):
            if before is None or after is None:
                raise EvidenceError("unpaired change in " + file["filename"])
            change = major_change(before, after)
            if change:
                raise EvidenceError(
                    f"major or unparsed version change in {file['filename']}: {change}"
                )
    for file in files:
        if file.get("head_type") != "file" or file.get("base_type") != "file":
            raise EvidenceError("not a regular file: " + file["filename"])
    if agent:
        agent_file_rules(files)


def agent_file_rules(files: list[Json]) -> None:
    """An agent's manifest change may only change version numbers.

    Dependabot's own diffs are trusted to be version bumps; an agent's are
    not, so every changed manifest line must be identical to the line it
    replaces apart from its version numbers.
    """
    for file in files:
        path = file["filename"]
        if lockfile(path):
            continue
        for before, after in changed_pairs(file["patch"]):
            assert before is not None and after is not None  # file_rules checked
            if VERSION.sub("#", before) != VERSION.sub("#", after):
                raise EvidenceError(f"agent changed more than a version in {path}")


def lock_rules(snapshot: Json, policy: Json) -> None:
    """Hold a lockfile change that moves a denied package or crosses a major."""
    agent = kind_of(snapshot["pr"], policy) == "agent"
    for file in snapshot["files"]:
        if not lockfile(file["filename"]):
            continue
        if agent:
            agent_lock_rules(file, policy)
        for name, (before, after) in sorted(lock_changes(file).items()):
            if denied_package(name, policy):
                raise EvidenceError(f"denied package {name} in {file['filename']}")
            # A package appearing or disappearing is a new or dropped
            # transitive dependency, not an upgrade of one already in use.
            # A package locked at several versions is compared by the set of
            # majors it is locked at, so a fork gaining a new major is held.
            if (
                before
                and after
                and {major(v) for v in before} != {major(v) for v in after}
            ):
                raise EvidenceError(
                    f"major version change in {file['filename']}: "
                    f"{name} {', '.join(sorted(before))} -> {', '.join(sorted(after))}"
                )


def review_state(snapshot: Json, policy: Json, committed_at: dt.datetime) -> str:
    trusted = policy["reviewer_id"]
    head = snapshot["pr"]["head"]["sha"]

    def mine(item: Json) -> bool:
        return (item.get("user") or {}).get("id") == trusted

    if any(mine(r) for r in snapshot["reviews"]) or any(
        mine(c) for c in snapshot["review_comments"]
    ):
        return "manual-review-findings"
    # The merging App bypasses the ruleset, so honour any reviewer whose most
    # recent decisive review (reviews arrive in submission order) requests changes.
    latest = {}
    for r in snapshot["reviews"]:
        if r.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[(r.get("user") or {}).get("id")] = r["state"]
    if "CHANGES_REQUESTED" in latest.values():
        return "changes-requested"
    reactions = [r for r in snapshot["reactions"] if mine(r)]
    thumbs = [timestamp(r["created_at"]) for r in reactions if r["content"] == "+1"]
    receipts = [
        timestamp(c["created_at"])
        for c in snapshot["comments"]
        if mine(c)
        and timestamp(c["created_at"]) > committed_at
        and (match := CLEAN_RECEIPT.match(c.get("body") or ""))
        and head.startswith(match[1])
    ]
    # A 👀 is running only if no clean signal followed it: Codex can leave an
    # old one behind when a later review finishes.
    latest_clean = max(thumbs + receipts, default=None)
    if any(
        r["content"] == "eyes"
        and (latest_clean is None or timestamp(r["created_at"]) >= latest_clean)
        for r in reactions
    ):
        return "review-running"
    # Clean evidence for H: a +1 newer than H's commit, or a clean receipt naming H.
    if not receipts and (not thumbs or max(thumbs) <= committed_at):
        return "review-stale"
    # Only the newest summary speaks for the current head; older ones describe
    # earlier commits.
    summaries = [
        c
        for c in snapshot["comments"]
        if mine(c) and SUMMARY_MARKER in (c.get("body") or "")
    ]
    if summaries:
        newest = max(summaries, key=lambda c: timestamp(c["created_at"]))
        match = SUMMARY_SHA.search(newest["body"])
        if not match or not head.startswith(match[1]):
            return "review-summary-mismatch"
    return "clean"


def required_contexts(snapshot: Json, policy: Json) -> list[str]:
    rules = snapshot["rules"]
    unknown = sorted({rule["type"] for rule in rules} - KNOWN_RULES)
    if unknown:
        raise EvidenceError("ci-unknown-rule: " + ", ".join(unknown))
    from_rules = {
        check["context"]
        for rule in rules
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    }
    return sorted(from_rules | set(policy["required_checks"]))


def status_state(snapshot: Json, name: str, creator: str) -> str:
    """Judge a required commit status by its newest report on the head."""
    reports = [s for s in snapshot["statuses"] if s["context"] == name]
    if not reports:
        return "ci-pending: " + name
    newest = max(reports, key=lambda s: (s["created_at"], s["id"]))
    if (newest["creator"] or {}).get("login") != creator:
        return "ci-untrusted-status: " + name
    if newest["state"] == "pending":
        return "ci-pending: " + name
    if newest["state"] != "success":
        return "ci-failure: " + name
    return "passed"


def ci_state(snapshot: Json, policy: Json) -> str:
    pr = snapshot["pr"]
    head = pr["head"]["sha"]
    try:
        contexts = required_contexts(snapshot, policy)
    except EvidenceError as error:
        return str(error)
    if not contexts:
        return "ci-no-required-checks"
    runs = snapshot["runs"]
    # A failure outranks a pending check, so keep looking past the first pending one.
    pending = None
    for name in contexts:
        if name in policy["statuses"]:
            state = status_state(snapshot, name, policy["statuses"][name])
            if state.startswith("ci-pending"):
                pending = pending or state
            elif state != "passed":
                return state
            continue
        workflow = policy["checks"].get(name)
        if not workflow:
            return "ci-unmapped-required-check: " + name
        candidates = [
            r
            for r in runs
            if r.get("path") == workflow
            and r.get("head_sha") == head
            and r.get("event") == "pull_request"
            and r.get("repository", {}).get("full_name") == policy["repository"]
            and any(p["number"] == pr["number"] for p in r.get("pull_requests", []))
        ]
        if not candidates:
            pending = pending or "ci-missing-trusted-run: " + name
            continue
        run = max(candidates, key=lambda r: (r["run_number"], r["run_attempt"]))
        jobs = snapshot["jobs"].get(str(run["id"]), [])
        matches = [
            j
            for j in jobs
            if j["name"] == name
            and j["run_attempt"] == run["run_attempt"]
            and j["head_sha"] == head
            and j["run_id"] == run["id"]
        ]
        if len(matches) != 1 or matches[0]["status"] != "completed":
            pending = pending or "ci-pending: " + name
            continue
        if matches[0]["conclusion"] != "success":
            return "ci-failure: " + name
    newest: dict[str, Json] = {}
    for run in runs:
        if run.get("head_sha") != head:
            continue
        key = run.get("path")
        if key not in newest or (run["run_number"], run["run_attempt"]) > (
            newest[key]["run_number"],
            newest[key]["run_attempt"],
        ):
            newest[key] = run
    for path, run in sorted(newest.items()):
        if run["status"] != "completed":
            pending = pending or "ci-pending: " + path
        elif run["conclusion"] in FAILED_CONCLUSIONS:
            return "ci-failure: " + path
    return pending or "passed"


NUDGEABLE = {"review-stale", "review-summary-mismatch"}

# Where a PR stands, shown on it as a label in merge mode. Worst wins:
#   queued  - eligible; merges when the lane is clear and pacing allows;
#   waiting - held by something that resolves without a person (CI running,
#             a Codex review pending or requested, mergeability computing);
#   blocked - could still merge automatically, but needs a person first
#             (CI failed, review unanswered, changes requested, CI wiring);
#   manual  - the policy never merges this PR; a person has to.
# A report whose evidence could not be read has no status, and its labels are
# left as they were.
STATUSES = ("queued", "waiting", "blocked", "manual")
STATUS_LABELS = {
    "queued": (
        "automerge: queued",
        "0e8a16",
        "Dependabot automerge: eligible, merges when the lane allows",
    ),
    "waiting": (
        "automerge: waiting",
        "fbca04",
        "Dependabot automerge: waiting on CI or a Codex review",
    ),
    "blocked": (
        "automerge: blocked",
        "d93f0b",
        "Dependabot automerge: needs a person before it can merge",
    ),
    "manual": (
        "automerge: manual",
        "5319e7",
        "Dependabot automerge: not eligible; merge by hand",
    ),
}
REVIEW_STATUS = {
    "clean": "queued",
    "review-running": "waiting",
    "review-stale": "waiting",
    "review-summary-mismatch": "waiting",
    "review-unanswered": "blocked",
    "changes-requested": "blocked",
    "manual-review-findings": "manual",
}


def ci_status(ci: str) -> str:
    if ci == "passed":
        return "queued"
    # A run that hasn't been created yet looks like a missing one.
    if ci.startswith(("ci-pending", "ci-missing-trusted-run")):
        return "waiting"
    return "blocked"


def review_evidence(snapshot: Json, policy: Json) -> Json:
    """What the review decision rested on, for the retained report."""

    def mine(item: Json) -> bool:
        return (item.get("user") or {}).get("id") == policy["reviewer_id"]

    evidence: Json = {
        "reactions": [
            {"id": r.get("id"), "content": r["content"], "created_at": r["created_at"]}
            for r in snapshot["reactions"]
            if mine(r)
        ],
        "summary": None,
        "receipts": [],
    }
    for comment in snapshot["comments"]:
        if mine(comment) and SUMMARY_MARKER in (comment.get("body") or ""):
            match = SUMMARY_SHA.search(comment["body"])
            evidence["summary"] = {
                "id": comment.get("id"),
                "commit": match[1] if match else None,
            }
        receipt = (
            CLEAN_RECEIPT.match(comment.get("body") or "") if mine(comment) else None
        )
        if receipt:
            evidence["receipts"].append({
                "id": comment.get("id"),
                "commit": receipt[1],
                "created_at": comment.get("created_at"),
            })
    return evidence


def review_request(comment: Json, policy: Json) -> bool:
    """A `@codex review` request from anyone but Codex (whose own text quotes it)."""
    return (comment.get("user") or {}).get("id") != policy[
        "reviewer_id"
    ] and "@codex review" in (comment.get("body") or "")


def nudge_due(snapshot: Json, policy: Json, committed_at: dt.datetime) -> str | None:
    """`nudge`, `unanswered`, or None when a review request is not due yet."""
    observed = timestamp(snapshot["observed_at"])
    requests = [
        timestamp(c["created_at"])
        for c in snapshot["comments"]
        if review_request(c, policy) and timestamp(c["created_at"]) > committed_at
    ]
    if requests:
        if observed - min(requests) > dt.timedelta(hours=24):
            return "unanswered"
        return None
    if observed - committed_at >= dt.timedelta(minutes=30):
        return "nudge"
    return None


def judge(snapshot: Json, policy: Json, report: Json) -> None:
    """Fill in `report`, raising EvidenceError at the first hold."""
    pr = snapshot.get("pr", {})
    if policy["mode"] not in {"report-only", "merge"}:
        raise EvidenceError("policy off or unsupported mode")
    if pr:
        # collect() stops at a failed admission and records it as an error,
        # but a veto such as the stop label is a policy decision.
        try:
            admission(pr, policy)
        except EvidenceError:
            report["status"] = "manual"
            raise
    if snapshot.get("errors"):
        raise EvidenceError("; ".join(snapshot["errors"]))
    # The evidence is complete, so a hold from here on is a policy decision.
    report["status"] = "manual"
    committed_at = provenance(snapshot, policy)
    report["committed_at"] = iso(committed_at)
    file_rules(snapshot, policy)
    lock_rules(snapshot, policy)
    report["review"] = review_state(snapshot, policy, committed_at)
    report["ci"] = ci_state(snapshot, policy)
    report["review_evidence"] = review_evidence(snapshot, policy)
    if report["review"] in NUDGEABLE and report["ci"] == "passed":
        due = nudge_due(snapshot, policy, committed_at)
        if due == "unanswered":
            report["review"] = "review-unanswered"
        elif due == "nudge":
            report["nudge"] = True
    report["status"] = max(
        REVIEW_STATUS[report["review"]],
        ci_status(report["ci"]),
        "queued" if pr.get("mergeable") is True else "waiting",
        key=STATUSES.index,
    )
    if report["review"] != "clean":
        raise EvidenceError(report["review"])
    if report["ci"] != "passed":
        raise EvidenceError(report["ci"])
    if pr.get("mergeable") is not True:
        raise EvidenceError("mergeability unknown or conflicting")
    report.update(decision="eligible", reason="all per-PR evidence satisfied")


def evaluate(snapshot: Json, policy: Json) -> Json:
    pr = snapshot.get("pr", {})
    report: Json = {
        "pr": pr.get("number"),
        "head": pr.get("head", {}).get("sha"),
        "base": pr.get("base", {}).get("sha"),
        "created_at": pr.get("created_at"),
        "mode": policy.get("mode"),
        "observed_at": snapshot.get("observed_at"),
        "check_run_ids": [r["id"] for r in snapshot.get("runs", [])],
        "kind": kind_of(pr, policy) if pr else None,
        "decision": "hold",
        "reason": "unavailable-evidence",
        "nudge": False,
        "status": None,
        "policy_sha256": policy_digest(policy),
    }
    try:
        judge(snapshot, policy, report)
    except (EvidenceError, KeyError, TypeError, ValueError, IndexError) as error:
        report["reason"] = str(error)
        if not isinstance(error, EvidenceError):
            report["status"] = None  # malformed evidence, not a policy decision
    return report


def policy_digest(policy: Json) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


# --- Lane rules: manual pause and pacing --------------------------------------
#
# There is deliberately no post-merge gate: CI on the PR already has to pass,
# and one update breaking something says little about the next. A person (or
# an agent responding to an alert) can stop the lane by opening an issue with
# the pause label.


def lane_state(lane: Json) -> Json:
    """clear, hold (evidence unavailable) or paused (an open pause issue)."""
    result: Json = {
        "state": "clear",
        "reasons": [],
        "last_auto_merge": lane.get("last_auto_merge"),
    }
    if lane.get("errors"):
        result.update(
            state="hold",
            reasons=["lane evidence unavailable: " + "; ".join(lane["errors"])],
        )
    elif lane["pause_issues"]:
        result.update(
            state="paused",
            reasons=[
                "open pause issue "
                + ", ".join(f"#{i['number']}" for i in lane["pause_issues"])
            ],
        )
    return result


def pacing(lane_result: Json, policy: Json, now: dt.datetime) -> str | None:
    window = policy["merge_window"]
    local = now.astimezone(ZoneInfo(window["timezone"]))
    if not window["start_hour"] <= local.hour < window["end_hour"]:
        return "outside merge window"
    last = lane_result.get("last_auto_merge")
    if last and now - timestamp(last) < dt.timedelta(
        minutes=policy["min_minutes_between_merges"]
    ):
        return "too soon after the previous automatic merge"
    return None


# --- Collection ---------------------------------------------------------------


def trim(item: Json, keys: tuple[str, ...]) -> Json:
    return {k: item.get(k) for k in keys}


RUN_KEYS = (
    "id",
    "path",
    "head_sha",
    "event",
    "run_number",
    "run_attempt",
    "status",
    "conclusion",
    "created_at",
    "html_url",
    "repository",
    "pull_requests",
)


def collect_discussion(prefix: str, number: int, snapshot: Json) -> None:
    for field, endpoint in (
        ("comments", f"issues/{number}/comments"),
        ("reviews", f"pulls/{number}/reviews"),
        ("review_comments", f"pulls/{number}/comments"),
        ("reactions", f"issues/{number}/reactions"),
    ):
        snapshot[field] = api(f"{prefix}/{endpoint}?per_page=100", True)
    snapshot["comments"] = [
        trim(c, ("id", "user", "body", "created_at")) for c in snapshot["comments"]
    ]
    snapshot["reviews"] = [
        trim(r, ("id", "user", "state", "commit_id")) for r in snapshot["reviews"]
    ]
    snapshot["review_comments"] = [
        trim(r, ("id", "user")) for r in snapshot["review_comments"]
    ]
    snapshot["reactions"] = [
        trim(r, ("id", "user", "content", "created_at")) for r in snapshot["reactions"]
    ]
    for item in (
        snapshot["comments"]
        + snapshot["reviews"]
        + snapshot["review_comments"]
        + snapshot["reactions"]
    ):
        item["user"] = trim(item["user"] or {}, ("id", "login"))


def collect_ci(prefix: str, head: str, policy: Json, snapshot: Json) -> None:
    snapshot["runs"] = [
        trim(r, RUN_KEYS)
        for r in api(
            f"{prefix}/actions/runs?head_sha={head}&per_page=100", True, "workflow_runs"
        )
    ]
    for run in snapshot["runs"]:
        run["repository"] = trim(run["repository"] or {}, ("full_name",))
        run["pull_requests"] = [
            trim(p, ("number",)) for p in run["pull_requests"] or []
        ]
    snapshot["statuses"] = [
        {
            **trim(s, ("id", "context", "state", "created_at")),
            "creator": trim(s["creator"] or {}, ("login",)),
        }
        for s in api(f"{prefix}/commits/{head}/statuses?per_page=100", True)
    ]
    snapshot["jobs"] = {}
    for run in snapshot["runs"]:
        if run["path"] in policy["checks"].values():
            snapshot["jobs"][str(run["id"])] = [
                trim(
                    j,
                    (
                        "name",
                        "run_id",
                        "run_attempt",
                        "head_sha",
                        "status",
                        "conclusion",
                    ),
                )
                for j in api(
                    f"{prefix}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100",
                    True,
                    "jobs",
                )
            ]


def collect_files(prefix: str, pr: Json, policy: Json, snapshot: Json) -> None:
    snapshot["files"] = []
    for f in api(f"{prefix}/pulls/{pr['number']}/files?per_page=100", True):
        file = trim(f, ("filename", "status"))
        if f.get("patch") is not None:
            file["patch"] = f["patch"]
        snapshot["files"].append(file)
    try:
        file_rules(
            dict(
                snapshot,
                files=[
                    dict(f, head_type="file", base_type="file")
                    for f in snapshot["files"]
                ],
            ),
            policy,
        )
    except EvidenceError:
        return  # already disqualified; skip the per-file reads
    for file in snapshot["files"]:
        for side, ref in (("head", pr["head"]["sha"]), ("base", pr["base"]["sha"])):
            path = f"{prefix}/contents/{quote(file['filename'])}?ref={ref}"
            file[side + "_type"] = api(path).get("type")
            if lockfile(file["filename"]):
                file[side + "_text"] = api(path, raw=True)


def gather(prefix: str, number: int, policy: Json, snapshot: Json) -> None:
    pr = snapshot["pr"] = api(f"{prefix}/pulls/{number}")
    admission(pr, policy)
    snapshot["commits"] = [
        {
            "sha": c["sha"],
            "committer": trim(c.get("committer") or {}, ("login",)),
            "commit": {
                "author": trim(c["commit"]["author"], ("email",)),
                "committer": trim(c["commit"]["committer"], ("date",)),
                "verification": trim(
                    c["commit"]["verification"], ("verified", "reason")
                ),
            },
        }
        for c in api(f"{prefix}/pulls/{number}/commits?per_page=100", True)
    ]
    collect_files(prefix, pr, policy, snapshot)
    collect_discussion(prefix, number, snapshot)
    collect_ci(prefix, pr["head"]["sha"], policy, snapshot)
    final = api(f"{prefix}/pulls/{number}")
    if final["head"]["sha"] != pr["head"]["sha"]:
        raise EvidenceError("head changed while gathering evidence")
    snapshot["pr"] = final


def collect(repository: str, number: int, policy: Json, rules: list[Json]) -> Json:
    prefix = "repos/" + repository
    snapshot: Json = {
        "observed_at": iso(dt.datetime.now(dt.UTC)),
        "errors": [],
        "rules": rules,
    }
    try:
        gather(prefix, number, policy, snapshot)
    except (
        EvidenceError,
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        OSError,
    ) as error:
        snapshot["errors"].append(str(error))
    pr = snapshot.get("pr")
    if pr:
        snapshot["pr"] = trim(
            pr,
            (
                "number",
                "state",
                "draft",
                "mergeable",
                "changed_files",
                "commits",
                "created_at",
                "labels",
                "user",
                "head",
                "base",
            ),
        )
        snapshot["pr"]["user"] = trim(pr["user"], ("id", "login"))
        snapshot["pr"]["labels"] = [trim(label, ("name",)) for label in pr["labels"]]
        for side in ("head", "base"):
            snapshot["pr"][side] = {
                "sha": pr[side]["sha"],
                "ref": pr[side]["ref"],
                "repo": trim(pr[side]["repo"] or {}, ("full_name",)),
            }
    return snapshot


def collect_lane(repository: str, policy: Json) -> Json:
    prefix = "repos/" + repository
    lane: Json = {"errors": [], "pause_issues": [], "last_auto_merge": None}
    try:
        label = urllib.parse.quote(policy["pause_label"])
        lane["pause_issues"] = [
            trim(i, ("number", "title"))
            for i in api(
                f"{prefix}/issues?state=open&labels={label}&per_page=100", True
            )
        ]
        merges = [
            item["timestamp"]
            for item in api(
                f"{prefix}/activity?ref=refs/heads/main&time_period=day&per_page=100",
                True,
            )
            if item["activity_type"] == "pr_merge"
            and (item.get("actor") or {}).get("login") == policy["merger_login"]
        ]
        lane["last_auto_merge"] = max(merges) if merges else None
    except (EvidenceError, KeyError, ValueError, TypeError, OSError) as error:
        lane["errors"].append(str(error))
    return lane


def discovered(repository: str, pr: Json, policy: Json) -> bool:
    """Every Dependabot PR, and agent PRs that change only dependency files.

    An agent opens many PRs; only those confined to the allowed files are
    dependency fixes, so the rest stay out of the report.
    """
    kind = kind_of(pr, policy)
    if kind != "agent":
        return kind == "dependabot"
    files = api(f"repos/{repository}/pulls/{pr['number']}/files?per_page=100", True)
    allowed = set(policy["agent"]["allowed_files"])
    return bool(files) and all(f["filename"] in allowed for f in files)


def required_rules(repository: str) -> list[Json]:
    return [
        trim(r, ("type", "parameters"))
        for r in api(f"repos/{repository}/rules/branches/main?per_page=100", True)
    ]


# --- Actions --------------------------------------------------------------------


def nudge(repository: str, policy: Json, requests: list[Json]) -> int:
    """Post one `@codex review` per head commit (GH_TOKEN: agent App)."""
    prefix = "repos/" + repository
    token = os.environ["GH_TOKEN"]
    failed = 0
    for request in requests:
        pr = api(f"{prefix}/pulls/{request['pr']}")
        if pr["head"]["sha"] != request["head"] or pr["state"] != "open":
            print(f"#{request['pr']}: head moved or PR closed; skipping")
            continue
        committed = api(f"{prefix}/commits/{request['head']}")["commit"]["committer"][
            "date"
        ]
        comments = api(f"{prefix}/issues/{request['pr']}/comments?per_page=100", True)
        if any(
            review_request(c, policy)
            and timestamp(c["created_at"]) > timestamp(committed)
            for c in comments
        ):
            print(
                f"#{request['pr']}: review already requested for {request['head'][:7]}"
            )
            continue
        code, output = write(
            "POST",
            f"{prefix}/issues/{request['pr']}/comments",
            {"body": "@codex review"},
            token,
        )
        print(
            f"#{request['pr']}: requested review for {request['head'][:7]} (exit {code})"
        )
        if code:
            print(output)
            failed += 1
    # Keep going for the other PRs, but a failed request must fail the job.
    return 1 if failed else 0


def label(repository: str, reports: list[Json]) -> int:
    """Give each evaluated PR exactly one status label (GH_TOKEN: workflow token).

    Runs in the evaluate job on that run's own reports, so a label always
    matches the report beside it. A closed PR (such as the one the merge job
    just merged) loses its status label on the next run.
    """
    prefix = "repos/" + repository
    token = os.environ["GH_TOKEN"]
    names = [name for name, _, _ in STATUS_LABELS.values()]
    failed = 0

    def change(
        number: int | str, method: str, endpoint: str, fields: dict[str, str]
    ) -> None:
        nonlocal failed
        code, output = write(method, endpoint, fields, token)
        if code:
            print(f"#{number}: {method} label failed: {output}")
            failed += 1

    def remove(number: int, name: str) -> None:
        change(
            number,
            "DELETE",
            f"{prefix}/issues/{number}/labels/{urllib.parse.quote(name, safe='')}",
            {},
        )

    existing = {item["name"] for item in api(f"{prefix}/labels?per_page=100", True)}
    for name, color, description in STATUS_LABELS.values():
        if name not in existing:
            change(
                "-",
                "POST",
                f"{prefix}/labels",
                {"name": name, "color": color, "description": description},
            )
    for report in reports:
        if not report.get("status") or not report.get("pr"):
            continue  # unreadable evidence: leave the labels as they were
        number = report["pr"]
        pr = api(f"{prefix}/pulls/{number}")
        wanted = STATUS_LABELS[report["status"]][0] if pr["state"] == "open" else None
        current = {entry["name"] for entry in pr["labels"]}
        stale = sorted(current & set(names) - {wanted})
        for name in stale:
            remove(number, name)
        if wanted and wanted not in current:
            change(
                number, "POST", f"{prefix}/issues/{number}/labels", {"labels[]": wanted}
            )
        if stale or (wanted and wanted not in current):
            print(f"#{number}: now {wanted or 'unlabelled (closed)'}")
    for name in names:
        for item in api(
            f"{prefix}/issues?state=closed&labels={urllib.parse.quote(name)}&per_page=100",
            True,
        ):
            if "pull_request" in item:
                remove(item["number"], name)
                print(f"#{item['number']}: closed; removed {name!r}")
    return 1 if failed else 0


def final_merge(repository: str, policy: Json, number: int, head: str) -> str:
    """Re-evaluate one PR and the lane from scratch, then merge at the expected head."""
    now = dt.datetime.now(dt.UTC)
    if policy["mode"] != "merge":
        return "hold: policy is not in merge mode"
    rules = required_rules(repository)
    report = evaluate(collect(repository, number, policy, rules), policy)
    if report["decision"] != "eligible" or report["head"] != head:
        return f"hold: {report['reason']} (head {report['head']})"
    if not acting(report["kind"], policy):
        return "hold: the policy only reports on this kind of PR"
    lane = lane_state(collect_lane(repository, policy))
    if lane["state"] != "clear":
        return "hold: lane " + lane["state"] + ": " + "; ".join(lane["reasons"])
    blocked = pacing(lane, policy, now)
    if blocked:
        return "hold: " + blocked
    code, output = write(
        "PUT",
        f"repos/{repository}/pulls/{number}/merge",
        {"sha": head, "merge_method": policy["merge_method"]},
        os.environ["MERGE_TOKEN"],
    )
    return f"merge exit {code}: {output.strip()[:500]}"


# --- Entry point ------------------------------------------------------------------


def markdown(
    reports: list[Json], lane: Json, candidate: Json | None, mode: str | None
) -> str:
    nudge_note = (
        " (review request queued for the nudge job)"
        if mode == "merge"
        else " (needs a review request; none posted in report-only)"
    )
    reasons = f" ({'; '.join(lane['reasons'])})" if lane["reasons"] else ""
    lines = [
        "# Dependabot automerge report",
        "",
        f"Mode: `{mode}`. Lane: **{lane['state']}**{reasons}.",
        f"Candidate: {('#' + str(candidate['pr'])) if candidate else 'none'}.",
        "",
    ]
    for report in reports:
        status = f" [{report['status']}]" if report.get("status") else ""
        kind = " (agent)" if report.get("kind") == "agent" else ""
        lines += [
            f"- PR #{report['pr']}{kind}{status}: **{report['decision']}** — {report['reason']}"
            + (nudge_note if report.get("nudge") else ""),
            f"  Head: `{report['head']}`; policy digest: `{report['policy_sha256'][:12]}`.",
        ]
        evidence = report.get("review_evidence")
        if evidence:
            thumbs = (
                ", ".join(
                    f"{r['content']} {r['id']} at {r['created_at']}"
                    for r in evidence["reactions"]
                )
                or "none"
            )
            summary = evidence["summary"]
            receipts = (
                ", ".join(
                    f"{r['id']} naming `{r['commit']}` at {r['created_at']}"
                    for r in evidence.get("receipts", [])
                )
                or "none"
            )
            lines.append(
                f"  Codex reactions: {thumbs}; summary comment: "
                + (
                    f"{summary['id']} naming `{summary['commit']}`"
                    if summary
                    else "none"
                )
                + f"; clean receipts: {receipts}."
            )
    return "\n".join(lines) + "\n"


def choose(
    reports: list[Json], lane: Json, policy: Json, now: dt.datetime
) -> Json | None:
    """Pick the oldest eligible PR, if the lane and pacing allow one."""
    for report in reports:
        # An agent PR the policy only reports on must not take the slot from
        # a Dependabot PR the lane could merge.
        if (
            report["decision"] == "eligible"
            and report.get("kind") == "agent"
            and policy["agent"]["mode"] != "merge"
        ):
            report["decision"] = "would-merge (agent lane is report-only)"
    eligible = sorted(
        (r for r in reports if r["decision"] == "eligible"),
        key=lambda r: r.get("created_at") or "",
    )
    if not eligible:
        return None
    blocked = pacing(lane, policy, now)
    if blocked:
        lane["reasons"].append(blocked)
        return None
    if lane["state"] == "clear":
        candidate = eligible[0]
        candidate["decision"] = (
            "would-merge" if policy["mode"] == "report-only" else "merge-candidate"
        )
        return candidate
    if policy["mode"] == "report-only":
        # Keep the per-PR result visible while the lane is paused or held.
        eligible[0]["decision"] = "would-merge-if-lane-clear"
    return None


def report_all(args: argparse.Namespace, policy: Json) -> int:
    now = dt.datetime.now(dt.UTC)
    lane: Json = {"state": "hold", "reasons": ["policy off"], "last_auto_merge": None}
    if policy.get("mode") not in {"report-only", "merge"}:
        snapshots: list[Json] = [{}]
    elif args.fixture:
        lane["reasons"] = ["fixture replay: lane not evaluated"]
        loaded = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        snapshots = [loaded] if isinstance(loaded, dict) else loaded
    else:
        try:
            rules = required_rules(args.repository)
            numbers = args.pr
            if numbers is None:
                numbers = [
                    p["number"]
                    for p in api(
                        f"repos/{args.repository}/pulls?state=open&per_page=100", True
                    )
                    if discovered(args.repository, p, policy)
                ]
            snapshots = [collect(args.repository, n, policy, rules) for n in numbers]
        except (EvidenceError, KeyError, TypeError, ValueError) as error:
            snapshots = [
                {
                    "observed_at": iso(now),
                    "errors": ["PR discovery unavailable: " + str(error)],
                }
            ]
        lane = lane_state(collect_lane(args.repository, policy))
    reports = [evaluate(snapshot, policy) for snapshot in snapshots]
    candidate = choose(reports, lane, policy, now)
    output = json.dumps({"lane": lane, "reports": reports}, indent=2) + "\n"
    if args.output_json:
        Path(args.output_json).write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    if args.output_markdown:
        Path(args.output_markdown).write_text(
            markdown(reports, lane, candidate, policy.get("mode")), encoding="utf-8"
        )
    if args.github_output:

        def lane_acts_on(report: Json) -> bool:
            return acting(report.get("kind"), policy)

        values = {
            "mode": policy.get("mode") or "off",
            "candidate": str(candidate["pr"])
            if candidate and lane_acts_on(candidate)
            else "",
            "candidate_head": candidate["head"]
            if candidate and lane_acts_on(candidate)
            else "",
            "nudge": json.dumps([
                {"pr": r["pr"], "head": r["head"]}
                for r in reports
                if r.get("nudge") and lane_acts_on(r)
            ]),
        }
        with open(args.github_output, "a", encoding="utf-8") as handle:
            for name, value in values.items():
                handle.write(f"{name}={value}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repository", default="werdnum/family-assistant")
    parser.add_argument("--policy", default=".github/dependabot-automerge-policy.json")
    parser.add_argument("--pr", action="append", type=int)
    parser.add_argument("--fixture")
    parser.add_argument("--output-json")
    parser.add_argument("--output-markdown")
    parser.add_argument("--github-output")
    parser.add_argument(
        "--nudge", help="JSON list of {pr, head} to request reviews for"
    )
    parser.add_argument(
        "--label", help="report JSON (from --output-json) to label PRs from"
    )
    parser.add_argument("--final", type=int, help="PR number to re-evaluate and merge")
    parser.add_argument("--head", help="expected head SHA for --final")
    args = parser.parse_args()
    policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
    if args.nudge is not None:
        return nudge(args.repository, policy, json.loads(args.nudge))
    if args.label is not None:
        if policy.get("mode") != "merge":
            print("not labelling: policy is not in merge mode")
            return 0
        reports = json.loads(Path(args.label).read_text(encoding="utf-8"))["reports"]
        return label(
            args.repository, [r for r in reports if acting(r.get("kind"), policy)]
        )
    if args.final is not None:
        result = final_merge(args.repository, policy, args.final, args.head)
        print(result)
        # A hold is a normal outcome; a refused merge call must fail the job.
        return (
            1
            if result.startswith("merge exit")
            and not result.startswith("merge exit 0:")
            else 0
        )
    return report_all(args, policy)


if __name__ == "__main__":
    sys.exit(main())
