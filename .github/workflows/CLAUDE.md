# CI Workflow Guide

This file provides guidance for working with GitHub Actions workflows.

## Workflow Files

- **`ci-with-devcontainer.yml`**: main CI. Builds the CI dev container image, then runs linting,
  frontend tests, and the backend suite against both SQLite and PostgreSQL, uploading test artifacts
  for each. Triggers on push to `main` and on pull requests. Internal runs exchange the CI image
  through GHCR. Fork PRs export a Docker image archive as a one-day workflow artifact, which
  `.github/actions/load-ci-image` downloads and loads in each consuming job. Fork runs do not log in
  to GHCR or export registry caches; live Gemini tests are skipped because fork tokens cannot access
  the API secret. Keep this on `pull_request`, without granting fork code a privileged token. The
  `all-tests` check includes setup and image builds so failed prerequisites cannot hide behind
  skipped test jobs.
- **`build-containers.yml`**: builds and pushes the application and devcontainer images.
- **`ios-tests.yml`**: iOS app tests (`workflow_call` / `workflow_dispatch`).
- **`auto-request-review.yml`**: requests reviewers when a PR is opened or marked ready.
- **`dependabot-automerge.yml`**: scheduled, review-gated merging of routine Dependabot PRs, driven
  by `scripts/dependabot_automerge.py` and `.github/dependabot-automerge-policy.json`. See
  [docs/design/dependabot-automerge.md](../../docs/design/dependabot-automerge.md).

## When Working on CI Workflows

Run the underlying commands locally first, and make one change at a time. `build-containers.yml` and
`ios-tests.yml` expose `workflow_dispatch` triggers you can use to exercise a run without pushing
commits.

## Debugging CI Failures

When CI tests fail, see [tests/CLAUDE.md](../../tests/CLAUDE.md) for CI debugging guidance:
downloading run artifacts, the artifact and JSON report names for each job, and interpreting
Playwright failure traces.
