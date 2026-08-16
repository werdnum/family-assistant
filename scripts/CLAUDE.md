# Scripts Development Guide

The `scripts/` directory contains utility scripts for development, testing, and deployment.

## Key Scripts

### `format-and-lint.sh`

Usage and the "must pass before committing" rule are covered in the root `AGENTS.md`. It routes each
argument by extension rather than accepting Python only: `.py` to ruff/basedpyright/pylint,
`.js/.jsx/.ts/.tsx/.vue` to Biome, ESLint and the TypeScript check, `.md` to mdformat, and
`.sh/.bash` to shellcheck. Pass frontend, documentation and script changes to it too — they are
checked, not ignored.

### `check_suppression_budget.py`

Enforces the shrink-only lint suppression budgets in `.lint-budget.toml`. For each budgeted rule it
asks ruff how many violations exist but go unreported: one run with the project config, one with
`--per-file-ignores` pointed at a path that matches nothing plus `--ignore-noqa`, and the difference
is the suppressed count. Enablement comes from `ruff check --show-settings`, so `preview`,
`extend-ignore` and selector precedence are all honoured.

Runs from `format-and-lint.sh` and as a pre-commit hook; takes filenames only so pre-commit can pass
them, and always counts the whole repository. The root `AGENTS.md` covers what to do when it fails.

### `check_mcp_servers.py`

Connects to the MCP servers named on the command line (or `--all`) through the same
`MCPToolsProvider` the application uses, and exits nonzero if any of them fails to come back
connected with at least one tool. Available as `poe check-mcp`.

This exists because a failed MCP server is otherwise invisible: the provider logs it, marks the
server `failed`, and the app keeps serving without those tools, so `/health` stays green.
`container-smoke-test.sh` runs it inside the built image so a release cannot ship a server that does
not start there.

Note that MCP stdio servers are spawned with a whitelisted environment — `HOME`, `LOGNAME`, `PATH`,
`SHELL`, `TERM`, `USER` — so a command cannot depend on anything else being exported to it. That is
why the servers are invoked by entry-point name rather than through `uvx`, which would need
`UV_TOOL_DIR` to find the environment the image installed and instead re-resolves from PyPI at every
startup.

### `container-smoke-test.sh`

Runs a built image, waits for `/health`, then verifies the MCP servers in `$MCP_SERVERS` (default
`time`) start inside it. Invoked by the `smoke-test` job in `build-containers.yml` via
`.github/actions/smoke-test-action`, which gates the push of the main image.

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

### `refresh-provider-model-skills.py`

Regenerates the current-model references for the Gemini, OpenAI, and Anthropic API development
skills from each provider's official public Markdown documentation. Pass `--check` to report drift
without writing files. The scheduled `refresh-provider-model-skills.yml` workflow opens a PR when
the generated snapshots change.

Gemini ids come from each detail page's "Model code" row, **not** the page's URL slug: the two
differ, and `gemini-omni-flash-preview` is served from a page slugged `gemini-omni-flash`. When a
page has no such row the slug is the only thing left, so that entry is written with
`"id_source": "url-slug (no documented Model code row)"` — treat those ids as unverified against the
API until a real call confirms them, rather than as equivalent to a documented code.

## Adding New Scripts

Development and deployment scripts go in `scripts/`; container build/run tooling goes in
`.devcontainer/`; test utilities go in `tests/`. Name shell scripts in lowercase with hyphens
(`build-and-push-container.sh`, not `build.sh`). Document the new script in this file and consider
adding a poe task for it in `pyproject.toml`.
