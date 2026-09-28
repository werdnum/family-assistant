"""Test React notes implementation using Playwright and Page Object Model."""

import urllib.parse

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.notes_page import NotesPage


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
async def test_react_ui_error_handling(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that the React UI handles errors gracefully."""
    page = web_test_fixture_readonly.page
    notes_page = NotesPage(page, web_test_fixture_readonly.base_url)

    # Test navigation to non-existent note for editing
    non_existent_title = "This_Note_Does_Not_Exist_12345"

    # Navigate directly to edit URL for non-existent note
    await notes_page.navigate_to_edit_note(non_existent_title)

    await expect(page.get_by_role("heading", name="Edit Note")).to_be_visible()
    await expect(page.get_by_role("alert")).to_contain_text("404")
    await expect(page.get_by_label("Title *")).to_be_empty()


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
