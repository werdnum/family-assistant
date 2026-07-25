# Scripts

Utility scripts for developing, testing, and deploying the Family Assistant. Roughly grouped:

- **Workspace setup** — `setup-workspace.sh` bootstraps a development environment;
  `install-parallel.sh` and `install-shellcheck.sh` fetch tools that setup depends on.
- **Lint and conformance** — `format-and-lint.sh` (ruff, basedpyright, pylint, conformance),
  `check-conformance.sh` for the ast-grep rules alone, and `audit-conformance-exemptions.sh` to list
  active exemptions.
- **Test running** — `run-tests.sh` is the entry point; `run_pytest_adaptive.py`,
  `run_pytest_shard.py`, `cgroup_memory_gate.py`, and `run_with_memory_limit.sh` implement the load-
  and memory-aware parallel runner. `failing-test-summary.sh` digests a JSON test report.
- **Test data and maintenance** — `validate_cassettes.py` checks VCR cassettes;
  `export-tool-calls.sh` and `prepare_test_data.py` turn production tool calls into anonymized
  fixtures; `mark_postgres_tests.py`, `mark_postgres_tests_simple.py`, and
  `migrate_tests_to_db_engine.py` are one-off codemods over the test suite.
- **Key and asset generation** — `generate_vapid_keys.py` (web push), `generate_credential_key.py`
  (credential encryption), `generate_tts_greeting.py` (Asterisk greeting audio).
- **Deployment and diagnostics** — `deploy-gke.sh`, `container-smoke-test.sh`, `wait-for-server.sh`,
  `wait-for-backend.sh`, `audit-sse-heartbeats.sh`.
- **Change review** — `review-changes.py` and `summarize-changes.sh` run the `llm` CLI over a diff.

## Development

See [CLAUDE.md](CLAUDE.md) in this directory for the development guide, including the environment
variables that tune the adaptive test runner and the conventions for adding a new script.
