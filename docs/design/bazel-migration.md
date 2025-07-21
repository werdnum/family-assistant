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

### In Progress

- [ ] Create BUILD files for subdirectory tests (telegram, web, indexing)

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
- **All 20 functional SQLite tests are now passing**
- Test utilities successfully migrated to `src/family_assistant/testing/`

### All Passing Tests (20/20)

- simple_test
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
- test_smoke_notes_fixed
- test_task_error_column
- test_task_worker_resilience
- test_vector_storage

### Notes

- Import paths resolved by moving test utilities to main package namespace
- Bazel's `imports` attribute properly configured for each BUILD file
- Warnings about deprecated Bazel conditions can be ignored (upstream issue)

## Bazel Commands

```bash
# Build the main application
bazel build //src/family_assistant:main

# Run all unit tests
bazel test //tests/unit:unit

# Run a specific test
bazel test //tests/unit:test_embeddings --test_output=all

# Run tests excluding postgres 
bazel test //... --config=sqlite

# Build everything
bazel build //...

# Clean build
bazel clean
```

## Next Steps

1. Create BUILD files for subdirectory tests (telegram, web, indexing)
2. Set up linting and formatting targets
3. Create CI configuration with Bazel
4. Handle PostgreSQL tests with testcontainers (complex, deferred)
5. Set up Docker image building with Bazel

## Notes

- Keeping existing tooling functional during migration
- Gradual adoption path for developers
- CI can use `bazel test //... --config=ci` to skip postgres tests
