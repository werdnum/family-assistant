"""Playwright-based functional tests for unified automations React UI."""

import pytest

from .conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_automations_page_basic_functionality(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test basic functionality of the automations React interface."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Navigate to automations page
    await page.goto(f"{server_url}/automations")

    # Wait for page to load
    await page.wait_for_selector("h1", timeout=10000)

    # Check page title and heading
    await page.wait_for_selector("h1:has-text('Automations')")

    # Check that both Create buttons are present
    event_button = page.locator("a:has-text('Create Event Automation')")
    await event_button.wait_for(timeout=5000)
    assert await event_button.is_visible()

    schedule_button = page.locator("a:has-text('Create Schedule Automation')")
    await schedule_button.wait_for(timeout=5000)
    assert await schedule_button.is_visible()

    # Check that filters section is present and expanded
    filters_summary = page.locator("details summary:has-text('Filters')")
    await filters_summary.wait_for(timeout=5000)
    details_element = page.locator("details:has(summary:has-text('Filters'))")
    await details_element.evaluate("details => { details.open = true; }")

    # Check filter dropdowns are present
    await page.wait_for_selector("select[name='type']", state="attached")
    await page.wait_for_selector("select[name='enabled']", state="attached")

    # Wait for the component to finish loading
    # It should show either "Loading automations...", an error, or the results
    await page.wait_for_selector(
        "text=/Loading automations|Error:|Found \\d+ automation(s)?/", timeout=10000
    )

    # If we see "Loading", wait a bit more
    if await page.locator("text=Loading automations").is_visible():
        await page.wait_for_selector(
            "text=/Error:|Found \\d+ automation(s)?/", timeout=10000
        )

    # Check results summary is present (should show 0 automations initially)
    # Skip this check if there's an error
    if not await page.locator("text=/Error:/").is_visible():
        results_summary = page.locator("text=/Found \\d+ automation(s)?/")
        await results_summary.wait_for(timeout=2000)
        summary_text = await results_summary.text_content()
        assert summary_text is not None
        assert "Found" in summary_text and "automation" in summary_text
    else:
        # There's an error - print it for debugging
        error_elem = page.locator("text=/Error:/")
        error_text = await error_elem.text_content()
        raise AssertionError(f"Component displayed error: {error_text}")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_automations_create_event_navigation(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test navigation to create new event automation form."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Navigate to automations page
    await page.goto(f"{server_url}/automations")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Automations')", timeout=10000)

    # Click Create Event Automation button
    create_button = page.locator("a:has-text('Create Event Automation')")
    await create_button.click()

    # Wait for navigation to new page
    await page.wait_for_url("**/automations/create/event", timeout=10000)

    # Verify we're on the create form page
    await page.wait_for_selector("h1:has-text('Create Event Automation')", timeout=5000)
    await page.wait_for_selector("form", timeout=5000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_automations_create_schedule_navigation(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test navigation to create new schedule automation form."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Navigate to automations page
    await page.goto(f"{server_url}/automations")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Automations')", timeout=10000)

    # Click Create Schedule Automation button
    create_button = page.locator("a:has-text('Create Schedule Automation')")
    await create_button.click()

    # Wait for navigation to new page
    await page.wait_for_url("**/automations/create/schedule", timeout=10000)

    # Verify we're on the create form page
    await page.wait_for_selector(
        "h1:has-text('Create Schedule Automation')", timeout=5000
    )
    await page.wait_for_selector("form", timeout=5000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_automations_filters_interaction(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test filter form interactions on automations page."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Navigate to automations page
    await page.goto(f"{server_url}/automations")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Automations')", timeout=10000)

    # Wait for the type filter dropdown to be ready
    filters_summary = page.locator("details summary:has-text('Filters')")
    await filters_summary.wait_for(state="visible", timeout=5000)
    details_element = page.locator("details:has(summary:has-text('Filters'))")
    await details_element.evaluate("details => { details.open = true; }")

    type_select = page.locator("select[name='type']")
    await type_select.wait_for(state="attached", timeout=5000)
    await type_select.select_option("event", force=True)
    selected_value = await type_select.input_value()
    assert selected_value == "event"

    # Test changing to schedule type
    await type_select.select_option("schedule", force=True)
    selected_value = await type_select.input_value()
    assert selected_value == "schedule"

    # Test enabled filter dropdown
    enabled_select = page.locator("select[name='enabled']")
    await enabled_select.select_option("true", force=True)
    selected_value = await enabled_select.input_value()
    assert selected_value == "true"

    # Test Clear Filters button
    clear_button = page.locator("button:has-text('Clear Filters')")
    await clear_button.click()

    # Wait for filters to be cleared by checking the type select value
    await page.wait_for_function(
        "() => { const el = document.querySelector('select[name=\"type\"]'); return el && el.value === 'all'; }",
        timeout=5000,
    )

    # Verify filters are cleared
    type_value = await type_select.input_value()
    enabled_value = await enabled_select.input_value()

    assert type_value == "all"
    assert not enabled_value


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_automations_responsive_design(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test responsive design of automations page."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Navigate to automations page
    await page.goto(f"{server_url}/automations")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Automations')", timeout=10000)

    # Test mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Check that main elements are still visible
    heading = page.locator("h1:has-text('Automations')")
    assert await heading.is_visible()

    event_button = page.locator("a:has-text('Create Event Automation')")
    assert await event_button.is_visible()

    schedule_button = page.locator("a:has-text('Create Schedule Automation')")
    assert await schedule_button.is_visible()

    # Test tablet viewport
    await page.set_viewport_size({"width": 768, "height": 1024})

    # Check elements are still visible
    assert await heading.is_visible()
    assert await event_button.is_visible()
    assert await schedule_button.is_visible()

    # Test desktop viewport
    await page.set_viewport_size({"width": 1200, "height": 800})

    # Check elements are still visible
    assert await heading.is_visible()
    assert await event_button.is_visible()
    assert await schedule_button.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_create_schedule_automation_form(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test creating a schedule automation via the form."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Navigate to create schedule automation page
    await page.goto(f"{server_url}/automations/create/schedule")

    # Wait for form to load
    await page.wait_for_selector(
        "h1:has-text('Create Schedule Automation')", timeout=10000
    )
    await page.wait_for_selector("form", timeout=5000)

    # Fill in the form
    await page.fill("input[name='name']", "Test Schedule Automation")
    await page.fill("input[name='recurrence_rule']", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0")
    await page.locator("button[role='combobox']").click()
    await page.locator('div[role="option"]:has-text("LLM Callback")').click()

    # Fill in action config
    await page.fill(
        "textarea[name='context']",
        "Generate a daily summary of my notes",
    )

    await page.click("button[type='submit']")

    # New UI navigates directly to the automation detail page after creation
    await page.wait_for_url("**/automations/schedule/*", timeout=10000)
    await page.wait_for_selector("text=/Test Schedule Automation/", timeout=5000)

    # Navigate back to the list and verify the automation shows up there as well
    await page.locator("a:has-text('Back to Automations')").click()
    await page.wait_for_url("**/automations", timeout=10000)
    await page.wait_for_selector("text=/Test Schedule Automation/", timeout=5000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_schedule_automation_detail_view(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test viewing schedule automation details."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # First create a schedule automation
    await page.goto(f"{server_url}/automations/create/schedule")
    await page.wait_for_selector("form", timeout=10000)

    await page.fill("input[name='name']", "Detail View Test")
    await page.fill("textarea[name='description']", "Test automation for detail view")
    await page.fill("input[name='recurrence_rule']", "FREQ=WEEKLY;BYDAY=MO")
    await page.locator("button[role='combobox']").click()
    await page.locator('div[role="option"]:has-text("LLM Callback")').click()
    await page.fill("textarea[name='context']", "Weekly reminder")

    await page.click("button[type='submit']")

    # After creation the UI routes straight to the detail page
    await page.wait_for_url("**/automations/schedule/*", timeout=10000)

    # Navigate back to the list to confirm the item is present and linking works
    await page.locator("a:has-text('Back to Automations')").click()
    await page.wait_for_url("**/automations", timeout=10000)
    await page.click("text=/Detail View Test/")
    await page.wait_for_url("**/automations/schedule/*", timeout=10000)

    # Verify detail page shows schedule-specific information
    await page.wait_for_selector("text=/Detail View Test/", timeout=5000)
    await page.wait_for_selector("text=/Test automation for detail view/")

    # Check for schedule-specific fields
    await page.wait_for_selector("text=/Recurrence Rule/")
    await page.wait_for_selector("text=/FREQ=WEEKLY;BYDAY=MO/")

    # Check for next scheduled time (should be present for schedule automations)
    await page.wait_for_selector("text=/Next Scheduled/")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_toggle_schedule_automation_enabled(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test toggling a schedule automation's enabled state."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Create a schedule automation
    await page.goto(f"{server_url}/automations/create/schedule")
    await page.wait_for_selector("form", timeout=10000)

    await page.fill("input[name='name']", "Toggle Test Automation")
    await page.fill("input[name='recurrence_rule']", "FREQ=DAILY;BYHOUR=10")
    await page.fill("textarea[name='description']", "Test toggle functionality")
    await page.locator("button[role='combobox']").click()
    await page.locator('div[role="option"]:has-text("LLM Callback")').click()
    await page.fill("textarea[name='context']", "Test")
    await page.click("button[type='submit']")

    # After creation we start on the detail page – toggle from here
    await page.wait_for_url("**/automations/schedule/*", timeout=10000)

    # Verify initial status shows enabled
    status_value = page.locator("dt:has-text('Status:') + dd")
    await status_value.wait_for()
    status_element = await status_value.element_handle()
    await page.wait_for_function(
        "el => el && el.textContent.includes('Enabled')",
        arg=status_element,
        timeout=5000,
    )

    disable_button = page.locator("button:has-text('Disable Automation')")
    await disable_button.click()

    await page.wait_for_function(
        "el => el && el.textContent.includes('Disabled')",
        arg=status_element,
        timeout=5000,
    )

    enable_button = page.locator("button:has-text('Enable Automation')")
    await enable_button.wait_for()
    await enable_button.click()

    await page.wait_for_function(
        "el => el && el.textContent.includes('Enabled')",
        arg=status_element,
        timeout=5000,
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_delete_schedule_automation(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test deleting a schedule automation."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Create a schedule automation
    await page.goto(f"{server_url}/automations/create/schedule")
    await page.wait_for_selector("form", timeout=10000)

    await page.fill("input[name='name']", "Delete Test Automation")
    await page.fill("input[name='recurrence_rule']", "FREQ=MONTHLY")
    await page.fill("textarea[name='description']", "This will be deleted")
    await page.locator("button[role='combobox']").click()
    await page.locator('div[role="option"]:has-text("LLM Callback")').click()
    await page.fill("textarea[name='context']", "Test")

    await page.click("button[type='submit']")

    # Creation lands on the detail page automatically
    await page.wait_for_url("**/automations/schedule/*", timeout=10000)

    # Click delete button
    delete_button = page.locator("button:has-text('Delete')")
    await delete_button.wait_for(timeout=5000)

    # Set up dialog handler to confirm deletion
    page.on("dialog", lambda dialog: dialog.accept())
    await delete_button.click()

    # Should navigate back to list
    await page.wait_for_url("**/automations", timeout=10000)

    # TODO(#TBD-replace-playwright-timeouts): replace wait_for_timeout with explicit wait
    # ast-grep-ignore: no-playwright-wait-for-timeout
    await page.wait_for_timeout(1000)
    assert not await page.locator("text=/Delete Test Automation/").is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_filter_schedule_automations(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test filtering to show only schedule automations."""
    page = web_test_with_console_check.page
    server_url = web_test_with_console_check.base_url

    # Create a schedule automation
    await page.goto(f"{server_url}/automations/create/schedule")
    await page.wait_for_selector("form", timeout=10000)

    await page.fill("input[name='name']", "Schedule Filter Test")
    await page.fill("textarea[name='description']", "For testing schedule filter")
    await page.fill("input[name='recurrence_rule']", "FREQ=DAILY")
    await page.locator("button[role='combobox']").click()
    await page.locator('div[role="option"]:has-text("LLM Callback")').click()
    await page.fill("textarea[name='context']", "Test")
    await page.click("button[type='submit']")
    await page.wait_for_url("**/automations/schedule/*", timeout=10000)

    # Return to the list before creating the event automation
    await page.locator("a:has-text('Back to Automations')").click()
    await page.wait_for_url("**/automations", timeout=10000)

    # Now also create an event automation for comparison
    await page.goto(f"{server_url}/automations/create/event")
    await page.wait_for_selector("form", timeout=10000)

    await page.fill("input[name='name']", "Event Filter Test")
    await page.fill("textarea[name='description']", "For testing event filter")
    await page.locator("button[role='combobox']").first.click()
    await page.locator("div[role='option']:has-text('Webhook')").click()
    await page.fill("textarea[name='match_conditions']", '{"event_type": "new_event"}')
    await page.locator("button[role='combobox']").last.click()
    await page.locator('div[role="option"]:has-text("LLM Callback")').click()
    await page.fill("textarea[name='context']", "Test")

    await page.click("button[type='submit']")
    await page.wait_for_url("**/automations/event/*", timeout=10000)

    # Go back to the list to perform filtering
    await page.locator("a:has-text('Back to Automations')").click()
    await page.wait_for_url("**/automations", timeout=10000)

    # Both automations should be visible
    await page.wait_for_selector("text=/Schedule Filter Test/", timeout=5000)
    await page.wait_for_selector("text=/Event Filter Test/", timeout=5000)

    # Filter to show only schedule automations
    filters_summary = page.locator("details summary:has-text('Filters')")
    await filters_summary.wait_for(state="visible", timeout=5000)
    details_element = page.locator("details:has(summary:has-text('Filters'))")
    await details_element.evaluate("details => { details.open = true; }")

    type_select = page.locator("select[name='type']")
    await type_select.select_option("schedule")

    # TODO(#TBD-replace-playwright-timeouts): replace wait_for_timeout with explicit wait
    # ast-grep-ignore: no-playwright-wait-for-timeout
    await page.wait_for_timeout(1000)

    # Schedule automation should be visible
    assert await page.locator("text=/Schedule Filter Test/").is_visible()

    # Event automation should not be visible
    assert not await page.locator("text=/Event Filter Test/").is_visible()

    # Filter to show only event automations
    await type_select.select_option("event")
    # TODO(#TBD-replace-playwright-timeouts): replace wait_for_timeout with explicit wait
    # ast-grep-ignore: no-playwright-wait-for-timeout
    await page.wait_for_timeout(1000)

    # Event automation should be visible
    assert await page.locator("text=/Event Filter Test/").is_visible()

    # Schedule automation should not be visible
    assert not await page.locator("text=/Schedule Filter Test/").is_visible()

    # Show all automations
    await type_select.select_option("all")
    # TODO(#TBD-replace-playwright-timeouts): replace wait_for_timeout with explicit wait
    # ast-grep-ignore: no-playwright-wait-for-timeout
    await page.wait_for_timeout(1000)

    # Both should be visible again
    assert await page.locator("text=/Schedule Filter Test/").is_visible()
    assert await page.locator("text=/Event Filter Test/").is_visible()
