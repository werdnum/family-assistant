# Web Functional Tests

This directory contains functional tests for the Family Assistant web UI.

## Running Tests

### With Bazel

Some tests work correctly with Bazel:

```bash
# Run all working web tests
bazel test //tests/functional/web:web

# Run individual working tests
bazel test //tests/functional/web:test_chat_api_endpoint
bazel test //tests/functional/web:test_notes_ui
bazel test //tests/functional/web:test_template_utils
bazel test //tests/functional/web:test_ui_endpoints
```

### With Pytest (Recommended for Playwright tests)

Tests that use relative imports (particularly Playwright tests) should be run with pytest directly:

```bash
# Run all web tests
pytest tests/functional/web/

# Run specific Playwright tests
pytest tests/functional/web/test_*playwright*.py
pytest tests/functional/web/test_enhanced_fixtures_simple.py
pytest tests/functional/web/test_history_flow.py
pytest tests/functional/web/test_documents_flow.py
pytest tests/functional/web/test_notes_flow.py
pytest tests/functional/web/test_page_object_basic.py
```

## Test Structure

- `conftest.py` - Shared fixtures for web tests
- `pages/` - Page Object Models for Playwright tests
- `test_*_ui.py` - UI endpoint tests (work with Bazel)
- `test_*_flow.py` - End-to-end flow tests (use pytest)
- `test_*playwright*.py` - Playwright-specific tests (use pytest)

## Known Issues

Tests using relative imports (e.g., `from .pages import BasePage`) don't work with Bazel due to
Python import path differences. These tests are marked with the "manual" tag in BUILD.bazel to
exclude them from automated test suites.
