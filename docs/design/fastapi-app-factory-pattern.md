# FastAPI App Factory Pattern for Test Isolation

**Date**: 2025-10-02 **Status**: Implemented **Related PR**:
[#267](https://github.com/werdnum/family-assistant/pull/267)

## Problem Statement

Tests were experiencing a ~30% failure rate with "no such table" SQLAlchemy errors when running with
pytest-xdist parallel execution. The failures were intermittent and difficult to reproduce
consistently.

### Symptoms

- `test_token_list_display`: ~30% failure rate
- `test_events_list_page_loads`: ~4% failure rate
- Errors like:
  `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: conversations`
- Failures only occurred with pytest-xdist (parallel execution), not in serial mode

### Investigation History

Initial hypotheses that were explored but ruled out:

1. **Health check timing**: Added health checks to ensure database was ready - didn't fix the issue
2. **WAL mode issues**: Tried forcing WAL mode for SQLite - didn't fix the issue
3. **Database initialization race**: Added startup events - didn't fix the issue

## Root Cause Analysis

The actual root cause was a **shared module-level singleton** causing state mutations across
concurrent test workers:

### The Problematic Pattern

```python
# app_creator.py (OLD)
app = FastAPI(...)  # Module-level singleton
app.state.config = {...}
app.state.database_engine = engine
# ... other state mutations

# assistant.py (OLD)
from family_assistant.web.app_creator import app as fastapi_app

class Assistant:
    async def setup_dependencies(self):
        # PROBLEM: All Assistant instances mutate the SAME app.state
        fastapi_app.state.config = self.config
        fastapi_app.state.database_engine = self.database_engine
        # ... etc
```

### Why This Caused Failures

1. **pytest-xdist** spawns multiple worker processes running tests in parallel
2. Each worker creates its own `Assistant` instance with its own test database
3. All Assistant instances imported and mutated the **same module-level `app` singleton**
4. Worker A sets `app.state.database_engine` to its SQLite database
5. Worker B immediately overwrites it with its own database
6. Worker A's tests try to query using Worker B's database engine
7. **Result**: "no such table" errors because tables exist in Worker A's DB but the engine points to
   Worker B's DB

### Why It Was Hard to Debug

- The race condition was timing-dependent
- Serial test execution worked fine (no concurrent mutation)
- The failure rate varied based on test execution order and timing
- Traditional debugging showed correct state at test start, but it changed during execution

## Solution: Factory Pattern

### Design Decision

Implement the **Factory Pattern** where each Assistant owns its isolated FastAPI instance:

1. Create a `create_app()` factory function that returns fresh FastAPI instances
2. Each Assistant gets its own `self.fastapi_app` - no shared state
3. Keep module-level `app` for backward compatibility (CLI, uvicorn)
4. Fix database engine lifecycle - only dispose if Assistant created it

### Implementation

```python
# app_creator.py (NEW)
def create_app() -> FastAPI:
    """Create a new FastAPI application instance.

    This factory function creates a fresh FastAPI instance with all middleware,
    routes, and configuration. Each instance has isolated state, preventing
    concurrent modifications when multiple apps are created (e.g., in tests).
    """
    new_app = FastAPI(...)
    new_app.state.templates = templates
    new_app.state.server_url = SERVER_URL
    # ... configure app
    return new_app

# Module-level singleton kept for backward compatibility
app = create_app()

# assistant.py (NEW)
from family_assistant.web.app_creator import create_app

class Assistant:
    def __init__(self, ...):
        self.fastapi_app: FastAPI | None = None
        self._injected_database_engine = database_engine

    async def setup_dependencies(self):
        # Create owned app instance - isolated state
        self.fastapi_app = create_app()
        self.fastapi_app.state.config = self.config
        self.fastapi_app.state.database_engine = self.database_engine
        # ... etc

    async def stop_services(self):
        # Only dispose engine if we created it (not if injected by tests)
        if self.database_engine and not self._injected_database_engine:
            await self.database_engine.dispose()
```

### Key Changes

1. **`create_app()` factory** (`app_creator.py`)

   - Returns fresh FastAPI instances with isolated state
   - Configures all middleware, routes, static files
   - Each call creates a new, independent app

2. **Instance ownership** (`assistant.py`)

   - Added `self.fastapi_app: FastAPI | None` instance variable
   - Create owned app in `setup_dependencies()` via `create_app()`
   - All state mutations go to owned instance

3. **Database lifecycle fix** (`assistant.py`)

   - Track injected vs. created engines with `self._injected_database_engine`
   - Only dispose engine if Assistant created it
   - Prevents disposing test fixtures' engines during teardown

4. **Test updates**

   - `conftest.py`: `actual_app` fixture returns `assistant.fastapi_app`
   - `test_conversation_history.py`: Use `web_only_assistant.fastapi_app` instead of module
     singleton

5. **Backward compatibility**

   - Module-level `app = create_app()` preserved for CLI usage
   - `uvicorn family_assistant.web.app_creator:app` still works
   - No breaking changes to deployment

## Results

### Flake-Finder Testing

Ran previously flaky tests 100-200 times each:

```bash
# Before: ~30% failure rate
pytest tests/functional/web/test_settings_ui.py::test_token_list_display \
  --flake-finder --flake-runs=100 -x
# Result: 100% pass rate (100/100)

# Before: ~4% failure rate
pytest tests/functional/web/test_events_ui.py::test_events_list_page_loads \
  --flake-finder --flake-runs=100 -x
# Result: 100% pass rate (200/200)
```

### Full Test Suite

```bash
poe test
# Result: 1138 passed, 2 skipped ✅
```

## Lessons Learned

1. **Module-level singletons are dangerous in parallel testing**

   - pytest-xdist creates true parallelism via multiprocessing
   - Module imports are shared across instances in the same process
   - State mutations affect all concurrent users

2. **The Factory Pattern is essential for test isolation**

   - Each test gets its own isolated instance
   - No shared mutable state
   - Parallel execution is safe

3. **Database lifecycle management matters**

   - Track ownership of injected vs. created resources
   - Only dispose resources you created
   - Test fixtures manage their own lifecycle

4. **Intermittent failures are often race conditions**

   - Look for shared mutable state
   - Consider concurrent access patterns
   - Use `--flake-finder` to verify fixes

5. **Backward compatibility can be maintained**

   - Keep module-level singleton for legacy usage
   - Add factory function for new code
   - Migrate incrementally

## Alternative Solutions Considered

### Option 1: Locks Around State Mutations

**Rejected**: Would slow down parallel tests and doesn't solve the fundamental problem of shared
state.

### Option 2: Per-Process App Instances

**Rejected**: Complex to implement, hard to reason about, and doesn't help within-process
concurrency.

### Option 3: Remove pytest-xdist

**Rejected**: Would dramatically slow down test suite. Parallel execution is valuable for CI speed.

### Option 4: Separate Databases Per Worker

**Rejected**: Already doing this - the problem was the shared app pointing to different databases
concurrently.

## Future Considerations

1. **Full Migration to Factory Pattern**

   - Consider deprecating module-level `app` entirely
   - Update all code to use `create_app()` explicitly
   - Remove backward compatibility shim

2. **Other Singleton Patterns**

   - Review codebase for other module-level singletons
   - Consider if any pose similar risks
   - Apply factory pattern proactively

3. **Testing Best Practices**

   - Document this pattern for future developers
   - Add linting rules to catch module-level FastAPI apps
   - Emphasize test isolation in contributor guidelines

## References

- [PR #267: Fix test flakiness by implementing FastAPI app factory pattern](https://github.com/werdnum/family-assistant/pull/267)
- [pytest-xdist documentation](https://pytest-xdist.readthedocs.io/)
- [Factory Pattern](https://en.wikipedia.org/wiki/Factory_method_pattern)
- [FastAPI Testing Best Practices](https://fastapi.tiangolo.com/tutorial/testing/)
