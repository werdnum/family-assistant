# Web Testing Guide

Guidance for the web API and UI tests.

## Layout

- **`api/`** — REST endpoint tests driven through an HTTPX `AsyncClient` against the FastAPI app. No
  browser.
- **`ui/`** — Playwright end-to-end tests, marked `@pytest.mark.playwright`.
- **`pages/`** — Page Object Models shared by the UI tests: `base_page.py` (`BasePage`),
  `chat_page.py` (`ChatPage`), `notes_page.py` (`NotesPage`), `events_page.py` (`EventsPage`),
  `history_page.py` (`HistoryPage`). Put selectors and multi-step interactions here rather than
  inline in tests.
- **`routers/`** — router-level tests that need neither browser nor full app.

## Documentation Screenshots

The `take_screenshot` fixture captures desktop (1920x1080) and mobile (393x852, iPhone 15 Pro)
viewports into `screenshots/{desktop,mobile}/`. It is a no-op unless `--take-screenshots` is passed.

```python
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_example(
    web_test_fixture: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    page = web_test_fixture.page
    await page.goto("/some-page")
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "page-name", viewport)
```

```bash
pytest tests/functional/web/ui/ --take-screenshots -xvs
```

See [screenshots/README.md](../../../screenshots/README.md).

## Playwright Locator Strictness

Playwright actions and waits run in strict mode when they imply a single target, so do not call
`wait_for()`, `click()`, `text_content()`, or similar on a locator that can legitimately match
several elements.

The concrete failure mode here: chat tests can have multiple `[data-testid="assistant-message"]`
elements once there is an initial assistant response and a later final one, so this line is flaky
and can fail before the real assertion runs:

```python
assistant_message = page.locator('[data-testid="assistant-message"]')
await assistant_message.wait_for(state="visible", timeout=10000)
```

Wait for the specific behaviour under test instead:

- `ChatPage.wait_for_message_content(...)` when the expected assistant/user text is what matters.
- A selector for the concrete UI being asserted, e.g. `[data-testid*="tool-call"]` or
  `[data-testid="tool-group"]`, when testing tool-call rendering.
- `.first` / `.last` / `.nth(index)` when the test genuinely needs one item from a multi-match
  locator — and assert the ordering assumption.
- `count()` or `expect(locator).to_have_count(...)` when you only need to prove at least one match
  exists.

## Debugging Playwright Failures

Failures write screenshots, video, and `trace.zip` to `test-results/`. `--screenshot on` and
`--video on` capture them for passing tests too. Open a trace with
`npx playwright show-trace test-results/*/trace.zip`.

Trace contents can also be grepped without the viewer, which is faster when you know what you are
looking for:

```bash
# Network requests / non-2xx responses
unzip -p test-results/*/trace.zip trace.network | strings | grep -A5 -B5 "send_message_stream"
unzip -p test-results/*/trace.zip trace.network | strings | grep "status.*[45][0-9][0-9]"

# Raw SSE payloads — use this to tell "server sent partial data" from "UI rendered partially"
unzip -p test-results/*/trace.zip resources/*.dat | head -50

# Browser console errors
unzip -p test-results/*/trace.zip trace.trace | strings | grep -i "error\|warning\|exception"
```

## Web API Test Fixtures

`mock_llm_client`, `app_fixture`, `web_test_fixture`, `take_screenshot`, `session_db_engine`, and
`api_db_context` come from `tests/functional/web/conftest.py`.

`db_context`, `mock_processing_service_config`, `test_tools_provider`, `test_processing_service`,
and `test_client` (an HTTPX `AsyncClient` over `app_fixture`) are declared per-file in `api/` tests
— copy them from a neighbouring test file rather than expecting them from a conftest.

```python
async def test_endpoint(test_client):
    response = await test_client.post("/api/endpoint", json={...})
    assert response.status_code == 200
```
