# CI Workflow Guide

This file provides guidance for working with GitHub Actions workflows.

## Workflow Files

- **`ci-with-devcontainer.yml`**: main CI. Builds the CI dev container image, then runs linting,
  frontend tests, and the backend suite against both SQLite and PostgreSQL, uploading test artifacts
  for each. Triggers on push to `main` and on pull requests.
- **`build-containers.yml`**: builds and pushes the application and devcontainer images.
- **`ios-tests.yml`**: iOS app tests (`workflow_call` / `workflow_dispatch`).
- **`gemini-dispatch.yml`** plus the other `gemini-*.yml` files: Gemini-driven triage, review, and
  plan/execute automation.
- **`auto-request-review.yml`**: requests reviewers when a PR is opened or marked ready.

## When Working on CI Workflows

Run the underlying commands locally first, and make one change at a time. `build-containers.yml` and
`ios-tests.yml` expose `workflow_dispatch` triggers you can use to exercise a run without pushing
commits.

## Debugging CI Failures

When CI tests fail, see [tests/CLAUDE.md](../../tests/CLAUDE.md) for CI debugging guidance:
monitoring runs with the `gh` CLI, downloading and analyzing artifacts, and interpreting test
reports.
