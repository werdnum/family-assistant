# Bazel Migration for Family Assistant

## Overview

This document tracks the migration of Family Assistant from setuptools/pyproject.toml to Bazel build
system using rules_uv for dependency management.

## Goals

1. Maintain existing pyproject.toml as source of truth for dependencies
2. Use rules_uv for fast dependency resolution
3. Get all non-PostgreSQL tests passing first
4. Defer PostgreSQL/testcontainers tests due to complexity
5. Provide hermetic, reproducible builds

## Progress Tracking

### Completed

- [x] Created MODULE.bazel with rules_python and rules_uv setup
- [x] Created .bazelrc with build and test configurations
- [x] Added SQLite-only test configuration (`test:sqlite`)
- [x] Updated .gitignore for Bazel artifacts
- [x] Created root BUILD.bazel with pip_compile rules
- [x] Set up rules_uv dependency compilation from pyproject.toml
- [x] Generated initial requirements.txt files
- [x] Created core library BUILD files for all packages
- [x] Successfully built core library and main binary
- [x] Created test BUILD files and configured pytest
- [x] Got unit tests passing (2/2 in //tests/unit)
- [x] Created py.typed file for proper package typing
- [x] Fixed Python import paths for Bazel's sandboxed environment
- [x] Migrated test utilities from tests/ to src/family_assistant/testing/
- [x] Fixed all import path issues
- [x] Added missing dependencies to BUILD files (e.g., janus)
- [x] Got all 20 functional SQLite tests passing
- [x] Created BUILD files for all test subdirectories:
  - [x] tests/functional/indexing/
  - [x] tests/functional/scripting/
  - [x] tests/functional/storage/
  - [x] tests/functional/telegram/
  - [x] tests/functional/tools/
  - [x] tests/functional/web/
  - [x] tests/data/
- [x] Split monolithic BUILD targets into granular per-module targets
- [x] All 33 SQLite functional tests passing with Bazel

### Todo

- [ ] Set up linting/formatting targets
- [ ] Create GitHub Actions workflow
- [ ] PostgreSQL/testcontainers support (deferred)

## Technical Decisions

### Using bzlmod (MODULE.bazel)

- Modern Bazel approach replacing WORKSPACE
- Better dependency management and version resolution
- Required for rules_uv which doesn't support WORKSPACE

### rules_uv for Dependencies

- Leverages fast uv package manager
- Compiles pyproject.toml to platform-specific requirements.txt
- Maintains pyproject.toml as single source of truth

### Test Strategy

- Focus on SQLite tests first (simpler, no external dependencies)
- PostgreSQL tests deferred due to testcontainers complexity
- Use test tags to separate test types

### Platform Support

- Multi-platform support (Linux, macOS)
- Platform-specific Python paths in .bazelrc
- Platform-specific dependency compilation with rules_uv

## Challenges

### Deferred: PostgreSQL/Testcontainers

- Complex Docker integration required
- Dynamic container lifecycle management
- Port allocation and networking
- Will tackle after core functionality works

### Resolved Challenges

1. **Python Import Paths in Bazel Sandbox**

   - Bazel runs tests in an isolated sandbox environment
   - Standard Python imports like `from tests.mocks import` fail
   - Solution: Migrated test utilities to `src/family_assistant/testing/`
   - This avoids namespace conflicts and provides proper package structure

2. **Missing Dependencies in BUILD Files**

   - Some packages were missing required dependencies (e.g., janus for events)
   - Had to track down and add these to the appropriate BUILD files
   - Solution: Run tests, check import errors, add missing deps

3. **py.typed File**

   - Bazel requires explicit py.typed file for proper package typing
   - Solution: Created `src/family_assistant/py.typed`

4. **Test Utility Import Issues**

   - Tests importing from `tests.mocks` or `tests.helpers` were failing
   - Generic "tests" namespace was likely conflicting
   - Solution: Moved all test utilities to `src/family_assistant/testing/`
   - Updated all imports using sed for automation

### Current Challenges

1. **Test Discovery**

   - Bazel requires explicit py_test targets for each test file
   - Cannot use pytest's automatic discovery
   - Need to create BUILD files for all test subdirectories

2. **Complex Test Dependencies**

   - Some tests require special environment setup (Docker, PostgreSQL)
   - CalDAV tests need Radicale server
   - MCP tests may need special configuration

## Current Status

### Working

- Basic Bazel build system is set up with rules_uv
- Core library builds successfully (`bazel build //src/family_assistant:family_assistant`)
- Main application binary builds (`bazel build //src/family_assistant:main`)
- Unit tests are passing (2/2 tests in //tests/unit)
- Dependencies are compiled from pyproject.toml using rules_uv
- Test utilities successfully migrated to `src/family_assistant/testing/`
- **All 33 functional SQLite tests are now passing with Bazel**
- BUILD files created for all test subdirectories with proper dependencies

### All Passing Tests (33/33)

**Unit Tests (2/2):**

- test_embeddings
- test_processing_history_formatting

**Functional Tests (31/31):**

- test_delegation
- test_error_logging
- test_event_listener_crud
- test_event_listener_script_tools
- test_event_listener_validation
- test_event_script_integration
- test_event_system
- test_note_tools
- test_notes_context_provider
- test_notes_prompt_inclusion
- test_recurring_task_timezone
- test_scheduled_script_execution
- test_script_execution_handler
- test_script_wake_llm
- test_smoke_callback
- test_smoke_notes
- test_task_error_column
- test_task_worker_resilience
- test_vector_storage

**Subdirectory Tests:**

- scripting: 7 tests (test_direct_tool_callables, test_engine, test_json_functions, test_time_api,
  test_time_integration, test_tools_api, test_tools_security)
- storage: 1 test (test_message_history)
- tools: 2 tests (test_execute_script, test_notes_append)
- web: 4 tests (test_chat_api_endpoint, test_notes_ui, test_template_utils, test_ui_endpoints)

### Architecture Improvements

1. **Granular BUILD Targets**: Split monolithic targets (e.g., `//src/family_assistant`) into
   per-module targets for:

   - Better build caching and parallelism
   - Clearer dependency graphs
   - Faster incremental builds

2. **Test Organization**: Created individual py_test targets for each test file with:

   - Proper dependency declarations
   - Test suites for grouping (sqlite, postgres, all)
   - Special handling for tests that don't work with Bazel (e.g., Playwright tests marked as
     "manual")

### Notes

- Import paths resolved by moving test utilities to main package namespace
- Bazel's `imports` attribute properly configured for each BUILD file
- Warnings about deprecated Bazel conditions can be ignored (upstream issue)
- PostgreSQL tests deferred but infrastructure is in place

## Bazel Commands

```bash
# Build the main application
bazel build //src/family_assistant:main

# Run all unit tests
bazel test //tests/unit:unit

# Run a specific test
bazel test //tests/unit:test_embeddings --test_output=all

# Run all SQLite tests (33 tests)
bazel test //tests/functional:sqlite

# Run tests in a specific subdirectory
bazel test //tests/functional/scripting:scripting

# Run tests excluding postgres 
bazel test //... --config=sqlite

# Build everything
bazel build //...

# Clean build
bazel clean
```

## Next Steps

1. Set up linting and formatting targets in Bazel
2. Create CI configuration with Bazel
3. Handle PostgreSQL tests with testcontainers (complex, deferred)
4. Set up Docker image building with Bazel
5. Migrate remaining test directories if any
6. Consider creating a `bazel run` target for the development server

## Notes

- Keeping existing tooling functional during migration
- Gradual adoption path for developers
- CI can use `bazel test //... --config=ci` to skip postgres tests
