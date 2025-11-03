"""Test React notes implementation using Playwright and Page Object Model."""

import urllib.parse

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.notes_page import NotesPage


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_notes_page_loads(web_test_fixture: WebTestFixture) -> None:
    """Test that the React notes page loads successfully."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Navigate to the React notes page
    await notes_page.navigate_to_notes_list()

    # Verify we're on the notes page
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Verify page has loaded by checking for key elements
    # The page should either show the empty state or notes table
    await page.wait_for_selector("body", timeout=10000)

    # The React app should render some content
    content = await page.text_content("body")
    assert content is not None and len(content.strip()) > 0, (
        "Page content should not be empty"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_create_note_full_flow(web_test_fixture: WebTestFixture) -> None:
    """Test complete note creation flow in React implementation."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Start with notes list
    await notes_page.navigate_to_notes_list()
    initial_count = await notes_page.get_note_count()

    # Navigate to add note form
    await notes_page.navigate_to_add_note()
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes/add")

    # Create a new note
    test_title = "React Test Note"
    test_content = "This is a test note created through the React UI."
    await notes_page.add_note(
        title=test_title, content=test_content, include_in_prompt=True
    )

    # Verify we're redirected back to the notes list
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Verify the note appears in the list
    assert await notes_page.is_note_present(test_title), (
        f"Note '{test_title}' should be present in the list"
    )

    # Verify note count increased
    new_count = await notes_page.get_note_count()
    assert new_count == initial_count + 1, (
        f"Note count should increase from {initial_count} to {initial_count + 1}, but got {new_count}"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_view_and_navigate_to_edit(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test viewing notes list and navigating to edit form."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # First create a note to view/edit
    original_title = "React Edit Test Note"
    original_content = "Original content for editing test"
    await notes_page.add_note(
        title=original_title, content=original_content, include_in_prompt=True
    )

    # Go back to notes list and verify the note is there
    await notes_page.navigate_to_notes_list()
    assert await notes_page.is_note_present(original_title), (
        f"Note '{original_title}' should be visible in the list"
    )

    # Click the edit link to navigate to edit form
    await notes_page.click_edit_note_link(original_title)

    # Verify we're on the edit page
    encoded_title = urllib.parse.quote(original_title)
    expected_edit_url = f"{web_test_fixture.base_url}/notes/edit/{encoded_title}"
    await expect(page).to_have_url(expected_edit_url)

    # Verify the form is pre-populated with note data
    note_data = await notes_page.get_note_content_from_edit_page(original_title)
    assert note_data["title"] == original_title, (
        f"Title should be '{original_title}', got '{note_data['title']}'"
    )
    assert note_data["content"] == original_content, (
        f"Content should be '{original_content}', got '{note_data['content']}'"
    )
    assert note_data["include_in_prompt"] is True, "Include in prompt should be True"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_edit_note_flow(web_test_fixture: WebTestFixture) -> None:
    """Test editing an existing note through the React UI."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # First create a note to edit
    original_title = "React Original Note"
    original_content = "Original content to be changed"
    await notes_page.add_note(
        title=original_title, content=original_content, include_in_prompt=True
    )

    # Edit the note with new content
    new_title = "React Updated Note"
    new_content = "This content has been updated through React!"
    await notes_page.edit_note(
        original_title=original_title,
        new_title=new_title,
        new_content=new_content,
        include_in_prompt=False,
    )

    # Verify we're redirected to the notes list
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Verify old title is gone and new title exists
    assert not await notes_page.is_note_present(original_title), (
        f"Old title '{original_title}' should not be present"
    )
    assert await notes_page.is_note_present(new_title), (
        f"New title '{new_title}' should be present"
    )

    # Verify the content was updated by navigating to edit page again
    note_data = await notes_page.get_note_content_from_edit_page(new_title)
    assert note_data["title"] == new_title, (
        f"Title should be '{new_title}', got '{note_data['title']}'"
    )
    assert note_data["content"] == new_content, (
        f"Content should be '{new_content}', got '{note_data['content']}'"
    )
    assert note_data["include_in_prompt"] is False, "Include in prompt should be False"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_delete_note_flow(web_test_fixture: WebTestFixture) -> None:
    """Test deleting a note through the React UI."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Create a note to delete
    title_to_delete = "React Note to Delete"
    await notes_page.add_note(
        title=title_to_delete, content="This note will be deleted from React UI"
    )

    # Verify the note exists and get initial count
    await notes_page.navigate_to_notes_list()
    assert await notes_page.is_note_present(title_to_delete), (
        f"Note '{title_to_delete}' should exist before deletion"
    )
    initial_count = await notes_page.get_note_count()

    # Delete the note
    await notes_page.delete_note(title_to_delete)

    # Verify we're still on the notes list
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Verify the note is gone
    assert not await notes_page.is_note_present(title_to_delete), (
        f"Note '{title_to_delete}' should be deleted"
    )

    # Verify count decreased
    new_count = await notes_page.get_note_count()
    assert new_count == initial_count - 1, (
        f"Note count should decrease from {initial_count} to {initial_count - 1}, but got {new_count}"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_navigation_between_pages(web_test_fixture: WebTestFixture) -> None:
    """Test navigation between different React notes pages."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Start at notes list
    await notes_page.navigate_to_notes_list()
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Navigate to add note page
    await notes_page.navigate_to_add_note()
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes/add")

    # Create a note for navigation testing
    test_title = "React Navigation Test"
    await notes_page.add_note(
        title=test_title,
        content="Testing navigation between pages",
        include_in_prompt=True,
    )

    # Should be back at notes list
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Navigate to edit page for the created note
    await notes_page.navigate_to_edit_note(test_title)
    # URL encode the title for comparison
    encoded_title = urllib.parse.quote(test_title)
    expected_edit_url = f"{web_test_fixture.base_url}/notes/edit/{encoded_title}"
    await expect(page).to_have_url(expected_edit_url)

    # Navigate back to notes list by URL (testing direct navigation)
    await notes_page.navigate_to_notes_list()
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Verify our test note is still there
    assert await notes_page.is_note_present(test_title), (
        f"Note '{test_title}' should still be present after navigation"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_empty_state_display(web_test_fixture: WebTestFixture) -> None:
    """Test that React UI shows appropriate empty state when no notes exist."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Navigate to notes list
    await notes_page.navigate_to_notes_list()

    # Check current note count
    note_count = await notes_page.get_note_count()

    if note_count == 0:
        # Verify empty state is visible
        assert await notes_page.is_empty_state_visible(), (
            "Empty state message should be visible when no notes exist"
        )
    else:
        # If there are existing notes, just verify they are displayed properly
        assert note_count > 0, "Should have notes displayed"
        assert not await notes_page.is_empty_state_visible(), (
            "Empty state should not be visible when notes exist"
        )

        # Get all note titles to verify they're properly rendered
        titles = await notes_page.get_all_note_titles()
        assert len(titles) == note_count, (
            f"Should have {note_count} note titles, but got {len(titles)}"
        )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_form_validation(web_test_fixture: WebTestFixture) -> None:
    """Test form validation in the React notes UI."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Navigate to add note form
    await notes_page.navigate_to_add_note()

    # Try to submit empty form
    await page.click(notes_page.SAVE_BUTTON)

    # Check for HTML5 validation on title field
    title_input = await page.wait_for_selector(notes_page.NOTE_TITLE_INPUT)
    if title_input:
        validation_message = await title_input.evaluate(
            "element => element.validationMessage"
        )
        assert validation_message, (
            "Title field should have validation message when empty"
        )

    # Fill only title and try to submit
    await notes_page.fill_form_field(notes_page.NOTE_TITLE_INPUT, "Title Only")
    await page.click(notes_page.SAVE_BUTTON)

    # Check content field validation
    content_textarea = await page.wait_for_selector(notes_page.NOTE_CONTENT_TEXTAREA)
    if content_textarea:
        content_validation = await content_textarea.evaluate(
            "element => element.validationMessage"
        )
        assert content_validation, (
            "Content field should have validation message when empty"
        )

    # Fill both required fields and submit
    await notes_page.fill_form_field(
        notes_page.NOTE_CONTENT_TEXTAREA, "Test content for validation"
    )
    await page.click(notes_page.SAVE_BUTTON)

    # Should succeed and redirect to notes list
    await page.wait_for_url(f"{web_test_fixture.base_url}/notes", timeout=10000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_checkbox_functionality(web_test_fixture: WebTestFixture) -> None:
    """Test that the include_in_prompt checkbox works correctly in React UI."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Test creating note with checkbox checked (default behavior)
    test_title_checked = "React Checkbox Test - Checked"
    await notes_page.add_note(
        title=test_title_checked,
        content="Testing checkbox checked state",
        include_in_prompt=True,
    )

    # Verify checkbox state in edit form
    note_data_checked = await notes_page.get_note_content_from_edit_page(
        test_title_checked
    )
    assert note_data_checked["include_in_prompt"] is True, (
        "Checkbox should be checked when set to True"
    )

    # Go back and create note with checkbox unchecked
    test_title_unchecked = "React Checkbox Test - Unchecked"
    await notes_page.add_note(
        title=test_title_unchecked,
        content="Testing checkbox unchecked state",
        include_in_prompt=False,
    )

    # Verify checkbox state in edit form
    note_data_unchecked = await notes_page.get_note_content_from_edit_page(
        test_title_unchecked
    )
    assert note_data_unchecked["include_in_prompt"] is False, (
        "Checkbox should be unchecked when set to False"
    )

    # Test toggling checkbox in edit form
    await notes_page.edit_note(
        original_title=test_title_unchecked,
        new_title=test_title_unchecked,  # Keep same title
        new_content="Updated content with toggled checkbox",
        include_in_prompt=True,  # Toggle to True
    )

    # Verify the toggle worked
    note_data_toggled = await notes_page.get_note_content_from_edit_page(
        test_title_unchecked
    )
    assert note_data_toggled["include_in_prompt"] is True, (
        "Checkbox should be checked after toggling"
    )
    assert note_data_toggled["content"] == "Updated content with toggled checkbox", (
        "Content should be updated"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_concurrent_operations(web_test_fixture: WebTestFixture) -> None:
    """Test that the React UI handles multiple operations gracefully."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Create multiple notes quickly to test React state management
    note_titles = [f"React Concurrent Note {i}" for i in range(3)]

    for title in note_titles:
        await notes_page.add_note(
            title=title,
            content=f"Content for {title} created in sequence",
            include_in_prompt=True,
        )

    # Verify all notes were created successfully
    await notes_page.navigate_to_notes_list()

    for title in note_titles:
        assert await notes_page.is_note_present(title), (
            f"Note '{title}' should be present after concurrent creation"
        )

    # Get final count to ensure all operations succeeded
    final_count = await notes_page.get_note_count()
    assert final_count >= len(note_titles), (
        f"Should have at least {len(note_titles)} notes, but got {final_count}"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_ui_error_handling(web_test_fixture: WebTestFixture) -> None:
    """Test that the React UI handles errors gracefully."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Test navigation to non-existent note for editing
    non_existent_title = "This_Note_Does_Not_Exist_12345"

    # Navigate directly to edit URL for non-existent note
    await notes_page.navigate_to_edit_note(non_existent_title)

    # The React app should handle this gracefully (either show error or redirect)
    # We'll verify the page doesn't crash and shows some meaningful content
    await page.wait_for_selector("body", timeout=10000)

    # Page should not be completely empty
    content = await page.text_content("body")
    assert content is not None and len(content.strip()) > 0, (
        "Page should show some content even for non-existent note"
    )

    # Check that no JavaScript errors occurred
    # (The conftest.py already sets up console error logging)
    # This test mainly ensures the React app doesn't crash


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_note_title_with_special_characters(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that React UI handles note titles with special characters correctly."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Test title with various special characters that are URL-safe but still test encoding
    special_title = "Test Note: With Special & Characters [2024]"
    special_content = (
        "This note has a title with special characters that need proper handling."
    )

    # Create note with special characters
    await notes_page.add_note(
        title=special_title, content=special_content, include_in_prompt=True
    )

    # Verify note appears in list
    assert await notes_page.is_note_present(special_title), (
        f"Note with special characters '{special_title}' should be present"
    )

    # Test navigation to edit page (this will test URL encoding/decoding)
    await notes_page.click_edit_note_link(special_title)

    # Verify we can access the edit page and data is preserved
    note_data = await notes_page.get_note_content_from_edit_page(special_title)
    assert note_data["title"] == special_title, (
        f"Title should be preserved: expected '{special_title}', got '{note_data['title']}'"
    )
    assert note_data["content"] == special_content, (
        f"Content should be preserved: expected '{special_content}', got '{note_data['content']}'"
    )

    # Test editing the note (roundtrip test)
    updated_content = "Updated content for note with special characters"
    await notes_page.edit_note(
        original_title=special_title,
        new_title=special_title,  # Keep same title
        new_content=updated_content,
        include_in_prompt=True,
    )

    # Verify the update worked
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")
    assert await notes_page.is_note_present(special_title), (
        "Note with special characters should still be present after edit"
    )
