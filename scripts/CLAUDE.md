# Scripts Development Guide

The `scripts/` directory contains utility scripts for development, testing, and deployment.

## Key Scripts

### `format-and-lint.sh`

Usage and the "must pass before committing" rule are covered in the root `AGENTS.md`. It routes each
argument by extension rather than accepting Python only: `.py` to ruff/basedpyright/pylint,
`.js/.jsx/.ts/.tsx/.vue` to Biome, ESLint and the TypeScript check, `.md` to mdformat, and
`.sh/.bash` to shellcheck. Pass frontend, documentation and script changes to it too — they are
checked, not ignored.

### `run-pylint.sh`

The single pylint invocation. `format-and-lint.sh` and `run-tests.sh` both go through it, so
`poe lint`, `poe test` and the CI lint job cannot enforce different rulesets -- they once did, with
`--errors-only` on one side only, which kept CI green while `poe test` failed. It takes paths and
refuses every option, so pylint is configured in `.pylintrc` and nowhere else. `.pylintrc` is the
whole configuration: pylint reads only the first config file it finds, so `[tool.pylint]` settings
in `pyproject.toml` are silently ignored.

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

Default pytest runner used by `scripts/run-tests.sh`. It collects nodeids, groups complete test
modules into serial pytest shards through GNU Parallel, and gates new shards on CPU load plus cgroup
v2 memory usage. Use `scripts/run-tests.sh --xdist-pytest` or
`PYTEST_RUNNER=xdist scripts/run-tests.sh` to use direct pytest-xdist instead.

Useful environment variables:

- `GNU_PARALLEL`: path to GNU Parallel when `parallel` is not on `PATH`
- `PYTEST_ADAPTIVE_BATCH_SIZE`: target backend nodeids per shard (whole modules stay together).
  Defaults to at least `25`, growing with the collection to target four shards per worker and
  amortize Python imports and session fixtures. Set this explicitly to tune the
  startup/load-balancing tradeoff. Tests marked `playwright` run first in batches capped at `25` so
  browser work remains distributed even in large modules. Markers and nodeids come from the final
  pytest collection, including selections made with `-m` and `-k`.
- `PYTEST_ADAPTIVE_JOBS`: maximum concurrent pytest shards, defaults to `12`
- `PYTEST_ADAPTIVE_MEM_THRESHOLD`: cgroup memory ratio that stops new shards, defaults to `0.80`
- `PYTEST_ADAPTIVE_LOAD`: GNU Parallel `--load` value, defaults to `100%`

### `refresh-provider-model-skills.py`

Mirrors each provider's official model documentation page into the Gemini, OpenAI, and Anthropic API
development skills as `references/current-models.md`. Pass `--check` to report drift without writing
files. The scheduled `refresh-provider-model-skills.yml` workflow opens a PR when a mirror changes.

The pages are copied **verbatim** — the providers publish them in an LLM-addressable form precisely
so consumers need not parse them, and the previous version of this script, which extracted records
into a schema of its own, was silently broken for months by routine upstream reformatting. Do not
reintroduce parsing here. If a consumer needs a different shape, it can read the page.

Each mirror carries a one-line header naming its source URL and the date it was taken; the body
below is untouched. The body **and** that URL are compared, so an unchanged page does not produce a
dated no-op commit, while a page that moves without changing content still updates the URL the
skills send readers to. mdformat is held off these files in both `.pre-commit-config.yaml` and this
script's own argument handling — reformatting them to our wrap width would make every refresh a diff
against our formatting rather than the provider's.

Providers are mirrored independently: one provider's moved page or outage leaves the others
refreshed, and the run exits non-zero naming the one that failed. The scheduled workflow runs the
refresh with `continue-on-error` so that a partial failure still publishes the mirrors that did
refresh, then fails the job in a final step.

### `restamp_legacy_definitions.py`

Operator command for the legacy definition amnesty: lists automations, event listeners and stored
scripts that hold no definition record and were created before a stated cutoff, and with `--apply`
records an amnesty for each. `--revoke` acts on the amnestied estate instead, clearing the grant.

It writes through `stamp_definition()` in the repository transaction that read the content, so the
hash covers what was amnestied and nothing hand-assembles a record — the same chokepoint the
conformance rule keeps every write path on. It fills absence only: a definition already holding a
record, a hash mismatch included, is skipped. `--created-before` has no default on purpose; a
definition written after stamping shipped with no record is a write-path regression, not a legacy
artifact.

Operator guidance lives in
[CONFIGURATION_REFERENCE.md](../docs/operations/CONFIGURATION_REFERENCE.md); the rationale is in
[docs/design/legacy-definition-amnesty.md](../docs/design/legacy-definition-amnesty.md).

### Review-eval public corpus scripts

`fetch_review_eval_corpora.sh` fetches pinned Deepset Prompt Injections and InjecAgent source files
into `.review-eval-local/upstream/`, then records revisions and SHA-256 checksums. It defaults to
the verified manifest commits, so a bare invocation is reproducible; optional
`--deepset-revision <sha>` and `--injecagent-revision <sha>` flags override the pins for an
intentional acquisition. The script never resolves a moving branch such as `main`. It requires Git,
Git LFS, and `shasum`; it refuses an existing output tree and publishes a fully staged fetch
atomically. If a fetch fails, fix the reported prerequisite or network/repository error and rerun it
with no pre-created `upstream/` directory.

`build_public_corpus_cases.py` turns one fetched source into schema-validated browser-ablation cases
under a fresh `.review-eval-local/public/<corpus>/` directory. `--out-dir` is resolved through the
private-tree containment guard and rejects tracked paths or symlink escapes. Use
`--evaluation-split dev` for iteration and `--evaluation-split gate` for held-out evidence;
InjecAgent additionally supports `--injecagent-variants base|enhanced|both`. The command refuses
every existing output path, reads the matching revision from the ancestor `upstream/manifest.txt`
(or requires an exact `--upstream-revision` for an input without a manifest), refuses a
contradictory explicit revision, validates the staged cases through the normal loader, and
atomically publishes cases plus provenance.

## Adding New Scripts

Development and deployment scripts go in `scripts/`; container build/run tooling goes in
`.devcontainer/`; test utilities go in `tests/`. Name shell scripts in lowercase with hyphens
(`build-and-push-container.sh`, not `build.sh`). Document the new script in this file and consider
adding a poe task for it in `pyproject.toml`.

### Batch review-eval runner

`tool_call_review_batch.py` is the staged private runner for OpenRouter's asynchronous batch
endpoint. `prepare` validates normal case inputs and writes deterministic request JSONL plus a
private manifest; `submit` is the only network-spending phase and requires both a finite positive
`--approved-spend-usd USD` and `--approve-spend`; `status` polls once, `poll` repeats it, and
`harvest` requires exact result reconciliation before writing a private `EvalReport`. Batch result
latency is explicitly unavailable, rather than inferred from polling time. See
`docs/development/tool-call-review-batch.md` for the operator runbook. Do not reuse an existing run
directory or resubmit a chunk whose POST outcome is unknown. The approved amount is recorded for
operator audit; it is not an enforceable provider-side spend cap.

`tool_call_review_gemini_batch.py` is the separate native Google Gemini Batch API runner. It uses
the same staged prepare/submit/poll/harvest workflow, but its manifest and uploaded/result JSONL are
provider-specific. It requires `GEMINI_API_KEY`, records positive operator approval without claiming
a provider spend cap, leaves failed or malformed uploads and definitive 4xx creation rejections
pending for retry, refuses ambiguous job-creation outcomes, and harvests only exactly reconciled
successful `STOP` responses. See `docs/development/tool-call-review-gemini-batch.md`; native batch
reports are review drafts until maintainers promote cases through the ordinary corpus workflow.
