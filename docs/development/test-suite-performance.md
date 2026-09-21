# Test suite performance

The default `poe test` runner and the backend CI jobs use the same adaptive pytest runner. It
collects selected tests and their browser markers once, then starts serial pytest processes through
GNU Parallel. Backend shards keep complete modules together; their target grows with the suite size
to leave roughly four scheduling waves per configured job slot, with a minimum target of 25 tests. A
backend module larger than the target stays intact. Browser tests start first in shards of at most
25 tests so their slower per-test work stays parallel in large collections. The existing CPU-load
and memory gates still control shard admission.

Use `PYTEST_ADAPTIVE_BATCH_SIZE` to override the target or `PYTEST_ADAPTIVE_JOBS` to bound
concurrent shards. More workers can increase contention rather than reduce elapsed time. Direct
xdist remains available with `poe test --xdist-pytest`.

SQLite database tests copy a closed, initialized schema into a fresh database file for every test.
Connections, transaction instrumentation, commits and teardown checks remain per test. The template
contains no test data; initialization and migration tests continue to exercise their own database
setup. PostgreSQL tests retain separate databases. Task-worker cleanup awaits worker completion
rather than adding fixed delays afterward. The test CalDAV server retains bcrypt authentication with
a test-only work factor of four; expensive password verification is not the behavior under test.

Frontend streaming tests override the existing retry-timing configuration to exercise retries
without waiting through production backoff. Retry counts and failure assertions remain intact. Chat
tests wait for the send action to become available instead of sleeping after a reply.

Shared task polling reads pending and failed counts in one database snapshot. Separate reads could
misreport success when a worker failed a task between them. Notification tests wait for their
specific task IDs, and the reconnection test scopes its asyncio stub to the event-source module
rather than changing asyncio for every task in the session.

## Measuring changes

Compare identical selections, database backends and worker limits. Run before/after commands
sequentially on an otherwise idle machine; dependency installation and an initial frontend build
should be reported separately from warm test execution. Use pytest's JSON reports and `--durations`
to separate setup, calls and teardown. Adaptive reports aggregate child-session durations, so use
the surrounding command's wall time or CI job timings for elapsed-time comparisons.

The optimization does not remove tests, reduce parametrization, skip either database backend, or
relax assertions. Fixture-isolation and shard-selection checks guard the shared setup and batching.

## Measurements

On the local macOS host, the complete frontend suite (490 tests) took 40.71 seconds before the
changes and 20.79 / 24.20 seconds afterward. Limiting the unchanged suite to two workers took 51.87
seconds, so the Vitest worker configuration remains unchanged.

An identical 204-test backend selection (`tests/unit/test_config_loader.py` and
`tests/unit/test_metrics.py`, SQLite, two adaptive job slots) took 27.75 seconds with main's runner
and 13.25 seconds with module-preserving shards. Pytest process count fell from nine to two; both
runs passed all 204 tests. The high load ceiling used for this comparison kept unrelated host load
from delaying shard admission.

A serial fixture comparison (`tests/functional/calendar/test_event_management.py` and
`tests/functional/notes/test_note_visibility.py`, both database backends) passed the same 50 tests
in 55.09 seconds before and 20.73 seconds after, including interpreter startup. This comparison
changes only `tests/conftest.py` against main and includes real PostgreSQL and CalDAV services.

The baseline CI run on main at `56f4ca4d4` was
[run 34109640363](https://github.com/werdnum/family-assistant/actions/runs/34109640363). Backend job
elapsed times were 1,280 seconds for SQLite and 1,602 seconds for PostgreSQL. Aggregated test setup
time was 1,530 and 3,173 seconds respectively, exceeding test-call time (471 and 782 seconds). These
totals overlap across workers and are not elapsed time.

The complete local `poe test --db all` run finished in 520 seconds (8m40s): 7,550 Python tests
passed, four were skipped, and all 490 frontend tests passed. Backend and frontend type checks and
frontend lint passed. The command exits nonzero only for existing Pylint W0012/W0231 warnings in
untouched test files; the required `scripts/format-and-lint.sh` checks pass. This is a full-run
measurement of the final configuration, not a before/after comparison against a full main run.
