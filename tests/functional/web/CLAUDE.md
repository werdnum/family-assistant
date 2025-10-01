# Playwright Web Testing Guide

This file provides guidance for working with Playwright end-to-end tests for the web UI.

## Overview

End-to-end tests for the web UI are written using Playwright and can be found in
`tests/functional/web/`. These tests are marked with `@pytest.mark.playwright`.

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
