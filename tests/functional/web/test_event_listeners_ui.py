"""Playwright-based functional tests for event listeners React UI."""

import pytest

from .conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_event_listeners_page_basic_functionality(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test basic functionality of the event listeners React interface."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to event listeners page
    await page.goto(f"{server_url}/event-listeners")

    # Wait for page to load
    await page.wait_for_selector("h1", timeout=10000)

    # Check page title and heading
    await page.wait_for_selector("h1:has-text('Event Listeners')")

    # Check that Create New Listener button is present
    create_button = page.locator("a:has-text('Create New Listener')")
    await create_button.wait_for(timeout=5000)
    assert await create_button.is_visible()

    # Check that filters section is present
    filters_section = page.locator("details summary:has-text('Filters')")
    await filters_section.wait_for(timeout=5000)
    assert await filters_section.is_visible()

    # Check filter dropdowns are present
    await page.wait_for_selector("select[name='source_id']")
    await page.wait_for_selector("select[name='action_type']")
    await page.wait_for_selector("select[name='enabled']")
    await page.wait_for_selector("input[name='conversation_id']")

    # Check results summary is present (should show 0 listeners initially)
    results_summary = page.locator("text=/Found \\d+ listener/")
    await results_summary.wait_for(timeout=5000)
    summary_text = await results_summary.text_content()
    assert summary_text is not None
    assert "Found" in summary_text and "listener" in summary_text


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_event_listeners_create_new_navigation(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test navigation to create new event listener form."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to event listeners page
    await page.goto(f"{server_url}/event-listeners")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Event Listeners')", timeout=10000)

    # Click Create New Listener button
    create_button = page.locator("a:has-text('Create New Listener')")
    await create_button.click()

    # Wait for navigation to new page
    await page.wait_for_url("**/event-listeners/new", timeout=10000)

    # Verify we're on the create form page
    # The form should have loaded (exact content depends on implementation)
    await page.wait_for_selector("form", timeout=5000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_event_listeners_filters_interaction(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test filter form interactions on event listeners page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to event listeners page
    await page.goto(f"{server_url}/event-listeners")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Event Listeners')", timeout=10000)

    # The details element should be open by default according to the component
    # Just wait a moment for React to fully render
    await page.wait_for_timeout(1000)

    # Test source filter dropdown - use force=True to interact even if not visible
    source_select = page.locator("select[name='source_id']")
    await source_select.select_option("home_assistant", force=True)
    selected_value = await source_select.input_value()
    assert selected_value == "home_assistant"

    # Test action type filter dropdown
    action_select = page.locator("select[name='action_type']")
    await action_select.select_option("wake_llm", force=True)
    selected_value = await action_select.input_value()
    assert selected_value == "wake_llm"

    # Test conversation ID input
    conv_input = page.locator("input[name='conversation_id']")
    await conv_input.fill("test_conversation", force=True)
    input_value = await conv_input.input_value()
    assert input_value == "test_conversation"

    # Test Clear Filters button
    clear_button = page.locator("button:has-text('Clear Filters')")
    await clear_button.click()

    # Wait for filters to be cleared by checking the source select value
    await page.wait_for_function(
        "() => { const el = document.querySelector('select[name=\"source_id\"]'); return el && el.value === ''; }",
        timeout=5000,
    )

    # Verify filters are cleared
    source_value = await source_select.input_value()
    action_value = await action_select.input_value()
    conv_value = await conv_input.input_value()

    assert not source_value
    assert not action_value
    assert not conv_value


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_event_listeners_responsive_design(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test responsive design of event listeners page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to event listeners page
    await page.goto(f"{server_url}/event-listeners")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Event Listeners')", timeout=10000)

    # Test mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Check that main elements are still visible
    heading = page.locator("h1:has-text('Event Listeners')")
    assert await heading.is_visible()

    create_button = page.locator("a:has-text('Create New Listener')")
    assert await create_button.is_visible()

    # Test tablet viewport
    await page.set_viewport_size({"width": 768, "height": 1024})

    # Check elements are still visible
    assert await heading.is_visible()
    assert await create_button.is_visible()

    # Test desktop viewport
    await page.set_viewport_size({"width": 1200, "height": 800})

    # Check elements are still visible
    assert await heading.is_visible()
    assert await create_button.is_visible()
