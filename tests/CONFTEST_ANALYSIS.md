# Test conftest.py Analysis Report

**Date**: 2025-11-03 **Scope**: Analysis of test directories to determine which need conftest.py
files

## Summary

After analyzing the test structure, **most directories do NOT need separate conftest.py files**
because:

- Tests within the same directory rarely share custom fixtures
- Test-specific fixtures are defined locally within test files
- All tests inherit common fixtures from parent conftest files

## Current conftest.py Files (8 total)

```
tests/conftest.py                              # Root fixtures: db_engine, postgres, vcr, etc.
├── tests/functional/conftest.py               # Auth disabling for functional tests
│   ├── tests/functional/web/conftest.py       # Web-specific fixtures (1170+ lines)
│   │   ├── tests/functional/web/api/conftest.py          # Empty
│   │   └── tests/functional/web/ui/conftest.py           # Empty
│   ├── tests/functional/telegram/conftest.py  # Telegram bot fixtures
│   └── tests/integration/conftest.py          # Empty (fixtures moved to subdir)
│       └── tests/integration/home_assistant/conftest.py  # HA fixtures
```

## Directories Analysis

### Unit Tests (14 directories, ~15 test files)

| Directory   | Tests | Fixtures           | Needs conftest.py? | Notes                                           |
| ----------- | ----- | ------------------ | ------------------ | ----------------------------------------------- |
| attachments | 3     | None shared        | ❌ No              | Pure unit tests, use db_engine from parent      |
| calendar    | 1     | None               | ❌ No              | Single test, inherits from parent               |
| events      | 6     | 1 fixture (local)  | ❌ No              | Events defined locally in test files            |
| indexing    | 6     | 1 fixture (local)  | ❌ No              | Indexing fixtures defined locally               |
| llm         | 1     | None               | ❌ No              | Single test                                     |
| processing  | 3     | 3 fixtures (local) | ❌ No              | Processing fixtures defined locally, not shared |
| services    | 1     | None               | ❌ No              | Single test                                     |
| storage     | 4     | 4 fixtures (local) | ❌ No              | Storage fixtures defined locally                |
| tools       | 2     | 2 fixtures (local) | ❌ No              | Tool fixtures defined locally                   |
| web         | 1     | None               | ❌ No              | Single test                                     |

**Summary**: All unit tests use parent fixtures or define fixtures locally. No shared fixtures
across test files.

### Functional Tests (18 directories, ~60 test files)

| Directory           | Tests | Fixtures                 | Needs conftest.py? | Notes                                                         |
| ------------------- | ----- | ------------------------ | ------------------ | ------------------------------------------------------------- |
| attachments         | 3     | 2 fixtures (test-local)  | ❌ No              | Fixtures defined in test files, not shared                    |
| automations         | 7     | 6+ fixtures (test-local) | ⚠️ Maybe           | Fixtures defined in test files, some patterns could be shared |
| calendar            | 5     | 3 fixtures (test-local)  | ❌ No              | Fixtures defined in test files                                |
| events              | 1     | None                     | ❌ No              | Single test                                                   |
| home_assistant      | 4     | None shared              | ❌ No              | Uses parent fixtures, test-specific logic local               |
| indexing            | 8     | 4 fixtures (test-local)  | ⚠️ Maybe           | Indexing fixtures could be shared, but not currently          |
| indexing/processors | 1     | 1 fixture (local)        | ❌ No              | Single test file                                              |
| integration         | 3     | 1 fixture (test-local)   | ❌ No              | MCP integration test-specific                                 |
| notes               | 4     | None shared              | ❌ No              | Tests use parent fixtures                                     |
| scripting           | 1     | 1 fixture (local)        | ❌ No              | Single test file                                              |
| storage             | 1     | None                     | ❌ No              | Single test                                                   |
| tasks               | 2     | None shared              | ❌ No              | Tests use parent fixtures                                     |
| telegram            | 1     | None                     | ❌ No              | Single test, has own conftest.py ✓                            |
| tools               | 2     | 2 fixtures (test-local)  | ❌ No              | Tool fixtures defined locally                                 |
| vector_search       | 2     | None shared              | ❌ No              | Tests use parent fixtures                                     |
| web                 | 0     | Multiple (parent-level)  | ✓ Yes              | Has conftest.py with fixtures                                 |
| web/api             | 6     | None shared              | ❌ No              | Has empty conftest.py, uses parent/api_client                 |
| web/ui              | 5     | None                     | ❌ No              | Has empty conftest.py, uses parent fixtures                   |

**Summary**: Most directories have tests with fixtures defined locally in test files, not shared
across files. Only `web/` directory has significant shared fixtures.

## Key Observations

### 1. Fixture Sharing Pattern

Most fixture definitions follow this pattern:

- **Test-specific fixtures**: Defined in the same test file that uses them
- **Shared by directory**: Rare - most directories don't have fixtures used across multiple test
  files
- **Shared across tests**: Common fixtures (db_engine, api_client) come from parent conftest

### 2. Existing Empty conftest.py Files

- `tests/functional/web/api/conftest.py` - Empty, but could inherit from parent
- `tests/functional/web/ui/conftest.py` - Empty, but could inherit from parent
- `tests/integration/conftest.py` - Empty placeholder, fixture moved to home_assistant subdir

### 3. Large Shared Fixture Files

- `tests/functional/web/conftest.py` - 1170+ lines with many fixtures
  - web_test_fixture, web_readonly_assistant, app_fixture, etc.
  - Needed because web testing has complex shared setup
  - Justifies dedicated conftest.py

## Recommendations

### 1. Don't Create New conftest.py Files

Follow the principle: "Only create conftest.py if there are actually shared fixtures needed"

**Rationale**:

- Adding empty or placeholder conftest files adds maintenance burden
- Actual shared fixtures should be defined when they emerge
- Current structure works well with parent conftest inheritance

### 2. If a Directory Needs Shared Fixtures

Create conftest.py with these guidelines:

- **Minimal scope**: Only include fixtures actually used by multiple test files
- **Clear documentation**: Add docstring explaining purpose
- **Proper nesting**: Place close to tests that use them
- **Avoid duplication**: Don't duplicate fixtures from parent conftest

### 3. For Empty conftest.py Files

Consider removing or populating:

- `tests/functional/web/api/conftest.py` - Empty, but present
- `tests/functional/web/ui/conftest.py` - Empty, but present

If they serve no purpose, they can be removed to reduce clutter.

## Conclusion

**No new conftest.py files are needed at this time.** The test structure is well-organized with:

- Parent conftest files providing common fixtures (db_engine, api_client, etc.)
- Test-specific fixtures defined locally in test files
- No clear patterns of shared fixtures within directories that lack conftest files

If in the future tests in a directory need shared fixtures, create a conftest.py at that time with
clear documentation of what fixtures are shared and why.

## Related Documents

- See tests/CLAUDE.md for testing patterns and fixture documentation
- See individual CLAUDE.md files in test subdirectories for domain-specific guidance
  - tests/functional/automations/CLAUDE.md
  - tests/functional/web/CLAUDE.md
  - tests/integration/CLAUDE.md
