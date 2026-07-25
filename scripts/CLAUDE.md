# Scripts Development Guide

The `scripts/` directory contains utility scripts for development, testing, and deployment.

## Key Scripts

### `format-and-lint.sh`

Usage and the "must pass before committing" rule are covered in the root `AGENTS.md`. It routes each
argument by extension rather than accepting Python only: `.py` to ruff/basedpyright/pylint,
`.js/.jsx/.ts/.tsx/.vue` to Biome, ESLint and the TypeScript check, `.md` to mdformat, and
`.sh/.bash` to shellcheck. Pass frontend, documentation and script changes to it too — they are
checked, not ignored.

### `run_pytest_adaptive.py`

Default pytest runner used by `scripts/run-tests.sh`. It collects nodeids, runs serial pytest shards
through GNU Parallel, and gates new shards on CPU load plus cgroup v2 memory usage. Use
`scripts/run-tests.sh --xdist-pytest` or `PYTEST_RUNNER=xdist scripts/run-tests.sh` to use direct
pytest-xdist instead.

Useful environment variables:

- `GNU_PARALLEL`: path to GNU Parallel when `parallel` is not on `PATH`
- `PYTEST_ADAPTIVE_BATCH_SIZE`: nodeids per shard, defaults to `25`
- `PYTEST_ADAPTIVE_JOBS`: maximum concurrent pytest shards, defaults to `12`
- `PYTEST_ADAPTIVE_MEM_THRESHOLD`: cgroup memory ratio that stops new shards, defaults to `0.80`
- `PYTEST_ADAPTIVE_LOAD`: GNU Parallel `--load` value, defaults to `100%`

## Adding New Scripts

Development and deployment scripts go in `scripts/`; container build/run tooling goes in
`.devcontainer/`; test utilities go in `tests/`. Name shell scripts in lowercase with hyphens
(`build-and-push-container.sh`, not `build.sh`). Document the new script in this file and consider
adding a poe task for it in `pyproject.toml`.
