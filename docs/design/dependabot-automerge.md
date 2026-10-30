# Dependabot: review-gated automatic merging

Status: shipped in `report-only` mode. Turning on merging is a one-line change to
`.github/dependabot-automerge-policy.json`, made once the setup below is done and a few reports look
right.

## Why

Dependabot opens grouped version updates monthly and security updates as advisories land. Merging
them is a click that is rarely preceded by close reading, and the PRs that pile up unmerged become
the backlog that the hand-made security sweep in #1534 had to clear. kube-config solved the same
problem for Renovate with a review-gated merge lane
([kube-config `docs/renovate-automerge-design.md`](https://github.com/werdnum/kube-config/blob/main/docs/renovate-automerge-design.md)):
over a month there, Codex flagged 7 of 53 dependency PRs, all real, and passed the rest. This ports
that lane rather than designing a new one.

## How it works

A scheduled workflow (`.github/workflows/dependabot-automerge.yml`) runs from `main` every 15
minutes and executes `scripts/dependabot_automerge.py`, which reads every open Dependabot PR and
reports a decision for each. It never checks out or runs PR code. A PR is merged only when all of
these hold:

- **Admission:** opened by `dependabot[bot]` from a `dependabot/` branch in this repository,
  targeting `main`, not a draft, without the `stop-updating` label.
- **Provenance:** exactly one commit, authored by Dependabot and signed by GitHub. A pushed fix
  makes the PR manual.
- **Files:** only plain modifications, nothing under `.github/`, `.devcontainer/` or `alembic/`, at
  most 10 files.
- **Versions:** no major version change, in a manifest line or in any package a lockfile resolves.
  No change to a denied package (the database layer, and `torch`/`sentence-transformers`, which CI
  does not install).
- **Review:** Codex has given the head commit a clean review (a 👍 newer than the commit, or a clean
  receipt naming it), has never left findings on the PR, and nobody has requested changes. When a
  review is missing or stale and CI has passed, the lane posts `@codex review` once per head commit,
  because Codex does not review on push and has not reliably reviewed PRs Dependabot opens.
- **CI:** `all-tests` passed on a `pull_request` run for this head, along with any check the branch
  ruleset requires, and no workflow run on the head failed.
- **Lane:** no open issue labelled `dependabot-automerge-paused`, inside 07:00–21:00 Sydney time,
  and at least an hour since the previous automatic merge. One PR merges per run, oldest first, with
  the expected head SHA, so a PR that changed after evaluation is not merged.

Missing or unreadable evidence is a hold, never a pass. In `merge` mode each PR also carries one
`automerge: queued|waiting|blocked|manual` label saying where it stands.

## Differences from kube-config

- **Admission and provenance** match Dependabot's identity rather than Renovate's.
- **Lockfiles are compared by resolved versions.** kube-config never merges a change that touches
  only `uv.lock`, because nothing in the diff bounds which transitive packages moved. Here that rule
  would hold nearly every Python PR: Dependabot's uv updates leave an already satisfied floor in
  `pyproject.toml` alone and change only `uv.lock`, and its transitive security fixes always do. So
  both sides of `uv.lock` and `package-lock.json` are parsed as data, and the PR is held if any
  package present on both sides crosses a major, or if a denied package moves. A package that
  appears or disappears is a new or dropped transitive dependency, not an upgrade.
- **Required checks** come from the policy (`all-tests`) as well as the branch ruleset, so the lane
  does not depend on how branch protection is configured.
- **Merge method** is `merge`, matching how this repository merges PRs.

## Setup before switching to `merge`

1. Install a GitHub App that can merge (contents: write) on this repository, and store its ID and
   private key as `AUTOMERGE_MERGE_APP_ID` / `AUTOMERGE_MERGE_PRIVATE_KEY` in an environment named
   `dependabot-automerge` whose deployment-branch policy allows only `main`. kube-config uses its
   image-updater App; `merger_login` in the policy names it, and must match whichever App is used,
   because pacing counts that App's merges.
2. Install an App whose comments Codex answers (kube-config uses its agent App) and store its
   credentials as the repository secrets `AUTOMERGE_REVIEW_APP_ID` / `AUTOMERGE_REVIEW_PRIVATE_KEY`.
   It needs issues: write.
3. If the branch ruleset requires a merge queue, the lane will report `ci-unknown-rule: merge_queue`
   and hold everything, because the merge call it makes bypasses the queue. Merging through the
   queue would be a change to the merge job.
4. Change `mode` to `merge`.

## Deliberate simplifications

- **0.x minors are not treated as majors.** The major check compares the leading version number
  only, as kube-config does. A `0.16 → 0.17` bump can break an API; CI and the Codex review are the
  guard, and kube-config's history shows no breakage from that class.
- **Lockfile-only security fixes are not special-cased.** They go through the same gates as any
  other update, so a transitive security fix that crosses a major waits for a person.
- **The script is a port, not a shared library.** Both repositories carry their own copy. They
  differ in the places listed above and in their CI wiring, and a shared copy would couple two
  repositories' merge credentials to one file.
