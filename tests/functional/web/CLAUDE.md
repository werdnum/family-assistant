# Web Testing Guide

This file provides guidance for working with web API and UI tests for the web application.

## Overview

Web tests are organized into two categories:

### API Tests (`tests/functional/web/api/`)

REST API endpoint tests that verify the backend API works correctly. These tests:

- Use the FastAPI test client to make HTTP requests
- Test request/response handling, status codes, and data validation
- Don't require Playwright or a browser
- Run quickly and test API contracts
- Include tests for automations, chat, file uploads, and more

Example tests:

- `test_chat_messages.py` - Chat API endpoint tests
- `test_chat_streaming.py` - Streaming chat response tests
- `test_automations_crud_api.py` - Automation CRUD operations
- `test_endpoints.py` - General endpoint tests

### UI Tests (`tests/functional/web/ui/`)

End-to-end browser tests using Playwright that verify the complete web UI works correctly. These
tests:

- Use Playwright to interact with the web UI like a real user
- Test page rendering, navigation, user interactions
- Are marked with `@pytest.mark.playwright`
- Include Page Object Models in `tests/functional/web/pages/` for reusable page interactions
- Run slower but provide the highest confidence in UI functionality

Example tests:

- `test_chat_basic.py` - Basic chat functionality
- `test_notes_ui.py` - Notes management
- `test_events_list.py` - Event listing and filtering
- `test_automations_ui.py` - Automation UI interactions

### Page Object Models (`tests/functional/web/pages/`)

Reusable Playwright page objects for common UI interactions. Helps maintain tests by abstracting
page structure and interactions into reusable classes.

Example page objects:

- `pages/chat.py` - ChatPage for chat UI interactions
- `pages/notes.py` - NotesPage for notes UI interactions
- `pages/sidebar.py` - SidebarPage for navigation

### Documentation Screenshots

Screenshots for documentation are captured throughout the test suite using the `take_screenshot`
fixture. Tests can call this fixture at key points to capture both desktop (1920x1080) and mobile
(393x852, iPhone 15 Pro) viewport screenshots.

**Usage in tests:**

```python
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_example(
    web_test_fixture: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    page = web_test_fixture.page

    # After navigating to a page
    await page.goto("/some-page")

    # Capture screenshots
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "page-name", viewport)
```

**Generating screenshots:**

```bash
# Run tests with screenshot capture enabled
pytest tests/functional/web/ui/ --take-screenshots -xvs

# Run specific test with screenshots
pytest tests/functional/web/ui/test_chat_basic.py --take-screenshots -xvs
```

Screenshots are saved to `screenshots/{desktop,mobile}/` and can be used in documentation. See
[screenshots/README.md](../../../screenshots/README.md) for details.

## Playwright UI Testing Guide

End-to-end tests for the web UI are written using Playwright and can be found in
`tests/functional/web/ui/`. These tests are marked with `@pytest.mark.playwright`.

## Debugging Playwright Tests

When a Playwright test fails, `pytest-playwright` automatically captures screenshots and records a
video of the test execution. These artifacts are invaluable for debugging.

- **Screenshots:** A screenshot is taken at the point of failure.
- **Videos:** A video of the entire test run is saved.
- **Traces:** Comprehensive debugging data including network requests, console logs, DOM snapshots,
  and action timeline.

By default, these are saved to the `test-results` directory. You can also use the `--screenshot on`
and `--video on` flags to capture these artifacts for passing tests as well.

## Advanced Debugging Techniques

### 1. Analyzing Network Traffic

```bash
# Extract and examine network requests from trace files
unzip -p test-results/*/trace.zip trace.network | strings | grep -A 5 -B 5 "send_message_stream"

# Look for specific API endpoints or error responses
unzip -p test-results/*/trace.zip trace.network | strings | grep "status.*[45][0-9][0-9]"
```

### 2. Examining Server-Sent Events (SSE) Streams

```bash
# Extract actual streaming response data to debug partial content issues
unzip -p test-results/*/trace.zip resources/*.dat | head -50

# This shows the actual SSE events and data sent by the server
# Useful for debugging streaming chat responses or real-time updates
```

### 3. Console Log Analysis

```bash
# Check for JavaScript errors or warnings
unzip -p test-results/*/trace.zip trace.trace | strings | grep -i "error\|warning\|exception"
```

### 4. Interactive Trace Viewing

```bash
# Open the full interactive trace viewer (requires Playwright CLI)
npx playwright show-trace test-results/*/trace.zip

# This provides a timeline view with:
# - Network requests and responses with full headers/body
# - Console messages with timestamps
# - DOM snapshots at each action
# - Screenshots at each step
```

### 5. Debugging Common Issues

- **Partial content/streaming issues:** Check SSE data extraction (method 2) to verify server sends
  complete data
- **Timing/race conditions:** Use trace timeline to see exact timing of actions vs. UI updates
- **Network failures:** Examine network requests for failed API calls or timeouts
- **DOM state issues:** Use DOM snapshots in trace viewer to see element state at failure point

## Playwright Artifacts in Detail

When Playwright tests fail, these artifacts are automatically generated:

- **Screenshots:** Capture the exact state when test failed
- **Videos:** Show the complete test execution, helpful for understanding test flow
- **Traces:** Comprehensive debugging data including:
  - Network requests and responses
  - Console logs and errors
  - DOM snapshots at each step
  - Action timeline with screenshots

Open traces with: `npx playwright show-trace trace.zip`

## Web API Testing Fixtures

These fixtures are available in various web API test files:

**`db_context`** (function scope)

- Provides a DatabaseContext for web API tests
- Usage: `async def test_api(db_context):`

**`mock_processing_service_config`** (function scope)

- Provides a ProcessingServiceConfig with test prompts

**`mock_llm_client`** (function scope)

- Provides a RuleBasedMockLLMClient for API tests

**`test_tools_provider`** (function scope)

- Configured ToolsProvider with local tools enabled

**`test_processing_service`** (function scope)

- ProcessingService instance with mock components

**`app_fixture`** (function scope)

- FastAPI application instance configured for testing

**`test_client`** (function scope)

- HTTPX AsyncClient for the test FastAPI app

- Usage:

  ```python
  async def test_endpoint(test_client):
      response = await test_client.post("/api/endpoint", json={...})
      assert response.status_code == 200
  ```
