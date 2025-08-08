"""Notes Page Object Model for Playwright tests."""

from typing import Any

from .base_page import BasePage


class NotesPage(BasePage):
    """Page object for notes-related functionality."""

    # Selectors
    ADD_NOTE_BUTTON = "a:has-text('Add New Note')"
    NOTE_TITLE_INPUT = "#title"
    NOTE_CONTENT_TEXTAREA = "#content"
    INCLUDE_IN_PROMPT_CHECKBOX = "input[name='include_in_prompt']"
    SAVE_BUTTON = "button:has-text('Save')"
    DELETE_BUTTON = "button[type='submit']:has-text('Delete')"
    SEARCH_INPUT = "#search-notes"
    NOTE_ROW = "tbody tr"
    NOTE_TITLE_IN_LIST = "td:first-child"
    EDIT_NOTE_LINK = "a:has-text('Edit')"
    NO_NOTES_MESSAGE = "text=No notes found"

    async def navigate_to_notes_list(self) -> None:
        """Navigate to the notes list page."""
        await self.navigate_to("/notes")
        await self.wait_for_load()

    async def ensure_on_notes_list(self) -> None:
        """Navigate to notes list only if not already there."""
        current_url = self.page.url.rstrip("/")
        expected_url = f"{self.base_url.rstrip('/')}/notes"
        if current_url != expected_url:
            await self.navigate_to_notes_list()

    async def navigate_to_add_note(self) -> None:
        """Navigate to the add note form."""
        await self.navigate_to("/notes/add")
        await self.wait_for_load()

    async def navigate_to_edit_note(self, title: str) -> None:
        """Navigate to edit a specific note.

        Args:
            title: The title of the note to edit
        """
        await self.navigate_to(f"/notes/edit/{title}")
        await self.wait_for_load()

    async def add_note(
        self, title: str, content: str, include_in_prompt: bool = True
    ) -> None:
        """Add a new note using the form.

        Args:
            title: The note title
            content: The note content
            include_in_prompt: Whether to include the note in prompts
        """
        await self.navigate_to_add_note()

        # Fill the form
        await self.fill_form_field(self.NOTE_TITLE_INPUT, title)
        await self.fill_form_field(self.NOTE_CONTENT_TEXTAREA, content)

        # Handle the checkbox
        checkbox = await self.page.wait_for_selector(self.INCLUDE_IN_PROMPT_CHECKBOX)
        if checkbox:
            is_checked = await checkbox.is_checked()
            if is_checked != include_in_prompt:
                await checkbox.click()

        # Submit the form
        await self.page.click(self.SAVE_BUTTON)
        await self.wait_for_load()

    async def edit_note(
        self,
        original_title: str,
        new_title: str | None = None,
        new_content: str | None = None,
        include_in_prompt: bool | None = None,
    ) -> None:
        """Edit an existing note.

        Args:
            original_title: The current title of the note
            new_title: Optional new title for the note
            new_content: Optional new content for the note
            include_in_prompt: Optional new setting for include in prompt
        """
        await self.navigate_to_edit_note(original_title)

        # Update fields if new values are provided
        if new_title is not None:
            await self.page.fill(self.NOTE_TITLE_INPUT, new_title)

        if new_content is not None:
            await self.page.fill(self.NOTE_CONTENT_TEXTAREA, new_content)

        if include_in_prompt is not None:
            checkbox = await self.page.wait_for_selector(
                self.INCLUDE_IN_PROMPT_CHECKBOX
            )
            if checkbox:
                is_checked = await checkbox.is_checked()
                if is_checked != include_in_prompt:
                    await checkbox.click()

        # Submit the form
        await self.page.click(self.SAVE_BUTTON)
        await self.wait_for_load()

    async def delete_note(self, title: str) -> None:
        """Delete a note.

        Args:
            title: The title of the note to delete
        """
        await self.ensure_on_notes_list()

        # Set up dialog handler before clicking delete
        self.page.on("dialog", lambda dialog: dialog.accept())

        # Find the row with the note and click its delete button
        note_row = await self.page.wait_for_selector(f"tr:has(td:text-is('{title}'))")
        if note_row:
            # In React version, delete button is a simple button, not in a form
            delete_button = await note_row.query_selector("button:has-text('Delete')")
            if delete_button:
                await delete_button.click()
                # Wait for the network request to complete and UI to update
                await self.wait_for_load(wait_for_network=True)

    async def search_notes(self, query: str) -> None:
        """Search for notes using the search input.

        Args:
            query: The search query
        """
        await self.ensure_on_notes_list()
        search_input = await self.page.wait_for_selector(self.SEARCH_INPUT)
        if search_input:
            await search_input.fill(query)
            # Trigger search by pressing Enter or waiting for debounce
            await self.page.keyboard.press("Enter")
            await self.wait_for_load()

    async def get_note_count(self) -> int:
        """Get the count of notes displayed on the page.

        Returns:
            The number of notes visible
        """
        await self.ensure_on_notes_list()
        # Count table rows, excluding the "No notes found" row
        note_rows = await self.page.query_selector_all(self.NOTE_ROW)
        # Check if it's the empty state
        if len(note_rows) == 1:
            first_row_text = await note_rows[0].text_content()
            if first_row_text and "No notes found" in first_row_text:
                return 0
        return len(note_rows)

    async def is_note_present(self, title: str) -> bool:
        """Check if a note with the given title is present in the list.

        Args:
            title: The title to search for

        Returns:
            True if the note is found, False otherwise
        """
        await self.ensure_on_notes_list()
        try:
            # Look for a table cell containing the exact title
            await self.page.wait_for_selector(f"td:text-is('{title}')", timeout=5000)
            return True
        except Exception:
            return False

    async def get_all_note_titles(self) -> list[str]:
        """Get all note titles from the list page.

        Returns:
            List of note titles
        """
        await self.ensure_on_notes_list()
        # Get all first cells in table rows (which contain titles)
        title_elements = await self.page.query_selector_all("tbody tr td:first-child")
        titles = []
        for element in title_elements:
            text = await element.text_content()
            if text and text.strip() != "No notes found.":
                titles.append(text.strip())
        return titles

    async def click_edit_note_link(self, title: str) -> None:
        """Click the edit link for a specific note from the list.

        Args:
            title: The title of the note to edit
        """
        await self.ensure_on_notes_list()
        # Find the row containing the title
        note_row = await self.page.wait_for_selector(f"tr:has(td:text-is('{title}'))")
        if note_row:
            # Click the Edit link in that row
            edit_link = await note_row.query_selector(self.EDIT_NOTE_LINK)
            if edit_link:
                await edit_link.click()
                await self.wait_for_load()

    async def is_empty_state_visible(self) -> bool:
        """Check if the empty state message is visible.

        Returns:
            True if the empty state is shown, False otherwise
        """
        await self.ensure_on_notes_list()
        return await self.is_element_visible(self.NO_NOTES_MESSAGE)

    async def get_note_content_from_edit_page(self, title: str) -> dict[str, Any]:
        """Get the content of a note from its edit page.

        Args:
            title: The title of the note

        Returns:
            Dictionary with note data (title, content, include_in_prompt)
        """
        await self.navigate_to_edit_note(title)

        title_input = await self.page.wait_for_selector(self.NOTE_TITLE_INPUT)
        content_textarea = await self.page.wait_for_selector(self.NOTE_CONTENT_TEXTAREA)
        checkbox = await self.page.wait_for_selector(self.INCLUDE_IN_PROMPT_CHECKBOX)

        note_data = {
            "title": await title_input.input_value() if title_input else "",
            "content": await content_textarea.input_value() if content_textarea else "",
            "include_in_prompt": await checkbox.is_checked() if checkbox else False,
        }

        return note_data
