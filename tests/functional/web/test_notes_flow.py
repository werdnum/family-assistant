"""Test complete notes management flows using Playwright and Page Object Model."""

import uuid

import pytest
from playwright.async_api import expect

from tests.functional.web.pages.notes_page import NotesPage

from .conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_create_note_full_flow(web_test_fixture: WebTestFixture) -> None:
    """Test complete note creation flow from UI."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Start with empty notes list
    await notes_page.navigate_to_notes_list()
    initial_count = await notes_page.get_note_count()

    # Add a new note
    test_title = "Test Note Creation"
    test_content = "This is a test note created through the UI."
    await notes_page.add_note(
        title=test_title, content=test_content, include_in_prompt=True
    )

    # Verify we're redirected to the notes list
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Verify the note appears in the list
    assert await notes_page.is_note_present(test_title)

    # Verify note count increased
    new_count = await notes_page.get_note_count()
    assert new_count == initial_count + 1

    # Click the note to edit and verify content
    await notes_page.click_edit_note_link(test_title)
    note_data = await notes_page.get_note_content_from_edit_page(test_title)

    assert note_data["title"] == test_title
    assert note_data["content"] == test_content
    assert note_data["include_in_prompt"] is True


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_edit_note_flow(web_test_fixture: WebTestFixture) -> None:
    """Test editing an existing note through the UI."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # First create a note to edit
    original_title = "Original Note Title"
    original_content = "Original content"
    await notes_page.add_note(
        title=original_title, content=original_content, include_in_prompt=True
    )

    # Edit the note with new content
    new_title = "Updated Note Title"
    new_content = "This content has been updated!"
    await notes_page.edit_note(
        original_title=original_title,
        new_title=new_title,
        new_content=new_content,
        include_in_prompt=False,
    )

    # Verify we're redirected to the notes list
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Verify old title is gone and new title exists
    assert not await notes_page.is_note_present(original_title)
    assert await notes_page.is_note_present(new_title)

    # Verify the content was updated
    note_data = await notes_page.get_note_content_from_edit_page(new_title)
    assert note_data["title"] == new_title
    assert note_data["content"] == new_content
    assert note_data["include_in_prompt"] is False


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_delete_note_flow(web_test_fixture: WebTestFixture) -> None:
    """Test deleting a note through the UI."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Create a note to delete
    title_to_delete = "Note to Delete"
    await notes_page.add_note(
        title=title_to_delete, content="This note will be deleted"
    )

    # Verify the note exists
    assert await notes_page.is_note_present(title_to_delete)
    initial_count = await notes_page.get_note_count()

    # Delete the note
    await notes_page.delete_note(title_to_delete)

    # Verify we're redirected to the notes list
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/notes")

    # Verify the note is gone
    assert not await notes_page.is_note_present(title_to_delete)

    # Verify count decreased
    new_count = await notes_page.get_note_count()
    assert new_count == initial_count - 1


@pytest.mark.flaky(reruns=3, reruns_delay=2)
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_search_notes_flow(web_test_fixture: WebTestFixture) -> None:
    """Test searching for notes through the UI."""

    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Use unique test ID to avoid conflicts with parallel tests
    test_id = str(uuid.uuid4())[:8]

    # Create some test notes with distinct titles for searching
    test_notes = [
        (f"TestGrocery{test_id}", "Buy milk, eggs, and bread"),
        (f"TestMeeting{test_id}", "Discussed project timeline and deliverables"),
        (f"TestRecipe{test_id}", "Try making pasta carbonara next week"),
        (f"TestShopping{test_id}", "Need new laptop and office supplies"),
        (f"TestThoughts{test_id}", "Reflecting on recent achievements"),
    ]

    # Add all test notes
    for title, content in test_notes:
        await notes_page.add_note(title=title, content=content, include_in_prompt=True)

    # Navigate to notes list and verify all notes exist
    await notes_page.navigate_to_notes_list()
    initial_count = await notes_page.get_note_count()
    assert initial_count >= len(test_notes)

    # Verify all notes are visible initially
    for title, _ in test_notes:
        assert await notes_page.is_note_present(title)

    # Test search functionality with different queries
    # Use the test_id to search for our specific test notes

    # Search for our test notes using the unique test ID
    await notes_page.search_notes(test_id)

    # Should show only our test notes
    filtered_count = await notes_page.get_note_count()
    assert filtered_count == len(test_notes)

    # Verify all our test notes are visible
    for title, _ in test_notes:
        assert await notes_page.is_note_present(title)

    # Search for "TestGrocery" - should show only grocery note
    await notes_page.search_notes("TestGrocery")

    filtered_count = await notes_page.get_note_count()
    assert (
        filtered_count >= 1
    )  # At least our grocery note, maybe others from parallel tests
    assert await notes_page.is_note_present(f"TestGrocery{test_id}")

    # Search for "TestMeeting" - should show only meeting note
    await notes_page.search_notes("TestMeeting")

    filtered_count = await notes_page.get_note_count()
    assert filtered_count >= 1  # At least our meeting note
    assert await notes_page.is_note_present(f"TestMeeting{test_id}")

    # Search for something that definitely doesn't exist
    await notes_page.search_notes(f"NonexistentNote{test_id}")

    filtered_count = await notes_page.get_note_count()
    assert filtered_count == 0

    # Clear search - should show all notes again
    await notes_page.search_notes("")

    final_count = await notes_page.get_note_count()
    assert final_count >= len(test_notes)

    # Verify all our original notes are visible again
    for title, _ in test_notes:
        assert await notes_page.is_note_present(title)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_empty_state_display(web_test_fixture: WebTestFixture) -> None:
    """Test that empty state is shown when no notes exist."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Navigate to notes list
    await notes_page.navigate_to_notes_list()

    # Check if we have any notes
    note_count = await notes_page.get_note_count()

    if note_count == 0:
        # Verify empty state is visible
        assert await notes_page.is_empty_state_visible()
    else:
        # If there are existing notes, create a new test that cleans them up first
        # For now, just verify that notes are displayed
        assert note_count > 0
        assert not await notes_page.is_empty_state_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_note_form_validation(web_test_fixture: WebTestFixture) -> None:
    """Test form validation for note creation."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Navigate to add note form
    await notes_page.navigate_to_add_note()

    # Try to submit empty form
    await page.click(notes_page.SAVE_BUTTON)

    # Check for validation messages (HTML5 validation)
    # The title field should be required
    title_input = await page.wait_for_selector(notes_page.NOTE_TITLE_INPUT)
    if title_input:
        validation_message = await title_input.evaluate(
            "element => element.validationMessage"
        )
    else:
        validation_message = ""
    assert validation_message  # Should have a validation message

    # Fill only title and try to submit
    await notes_page.fill_form_field(notes_page.NOTE_TITLE_INPUT, "Title Only")
    await page.click(notes_page.SAVE_BUTTON)

    # Check content field validation (also required)
    content_textarea = await page.wait_for_selector(notes_page.NOTE_CONTENT_TEXTAREA)
    if content_textarea:
        content_validation = await content_textarea.evaluate(
            "element => element.validationMessage"
        )
    else:
        content_validation = ""
    assert content_validation  # Content is also required

    # Fill both fields and submit
    await notes_page.fill_form_field(notes_page.NOTE_CONTENT_TEXTAREA, "Test content")
    await page.click(notes_page.SAVE_BUTTON)

    # Should succeed now - wait for redirect to notes list
    await page.wait_for_url(f"{web_test_fixture.base_url}/notes")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_concurrent_note_operations(web_test_fixture: WebTestFixture) -> None:
    """Test that the UI handles concurrent operations gracefully."""
    page = web_test_fixture.page
    notes_page = NotesPage(page, web_test_fixture.base_url)

    # Create multiple notes quickly
    note_titles = [f"Concurrent Note {i}" for i in range(5)]

    for title in note_titles:
        await notes_page.add_note(
            title=title, content=f"Content for {title}", include_in_prompt=True
        )

    # Verify all notes were created
    for title in note_titles:
        assert await notes_page.is_note_present(title)

    # Get final count
    final_count = await notes_page.get_note_count()
    assert final_count >= len(note_titles)
