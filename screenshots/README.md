# UI Screenshots for Documentation

This directory contains screenshots captured during Playwright tests for documentation purposes.

## Organization

Screenshots are organized by viewport type:

- **desktop/** - Desktop screenshots (1920x1080)
- **mobile/** - Mobile screenshots (393x852, iPhone 15 Pro size)

## How It Works

Screenshots are captured by adding calls to the `take_screenshot` fixture in Playwright tests:

```python
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_example(
    web_test_fixture: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    page = web_test_fixture.page

    # Navigate to a page and interact with it
    await page.goto("/some-page")

    # Capture screenshots for both viewports
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "my-page-name", viewport)
```

## Generating Screenshots

Screenshots are only captured when the `--take-screenshots` flag is passed:

```bash
# Run tests and generate all screenshots
pytest tests/functional/web/ui/ --take-screenshots -xvs

# Run specific test and generate screenshots
pytest tests/functional/web/ui/test_chat_basic.py::test_basic_chat_conversation --take-screenshots -xvs
```

Without the flag, screenshot calls are no-ops, so tests run normally.

## Current Coverage

Screenshots are currently captured in these tests:

- **test_chat_basic.py**: Chat interface (empty, with messages, sidebar)
- **test_landing_page.py**: Landing page with feature cards
- More tests will be added over time

## Adding Screenshots to New Tests

When writing or updating tests, consider adding screenshot calls at key points:

1. After navigating to a new page
2. After completing a significant interaction
3. After data is loaded and displayed
4. For different UI states (empty, populated, error, etc.)

The goal is to build a comprehensive visual documentation library of the application UI.
