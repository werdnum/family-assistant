"""Documents Page Object Model for Playwright tests."""

import json
from typing import Any

from .base_page import BasePage


class DocumentsPage(BasePage):
    """Page object for document management functionality."""

    # Selectors for documents list page
    UPLOAD_LINK = "a:has-text('Upload your first document')"
    DOCUMENT_ROW = "tbody tr"
    VIEW_LINK = "a:has-text('View')"
    SOURCE_LINK = "a:has-text('Source')"
    REINDEX_BUTTON = "button:has-text('Re-index')"
    NO_DOCUMENTS_MESSAGE = "text=No documents found"
    PAGINATION_PREV = "a:has-text('← Previous')"
    PAGINATION_NEXT = "a:has-text('Next →')"
    TOTAL_COUNT_TEXT = "p:has-text('Total documents:')"

    # Selectors for upload form
    SOURCE_TYPE_INPUT = "#source_type"
    SOURCE_ID_INPUT = "#source_id"
    SOURCE_URI_INPUT = "#source_uri"
    TITLE_INPUT = "#title"
    CREATED_AT_INPUT = "#created_at"
    METADATA_TEXTAREA = "#metadata"
    CONTENT_PARTS_TEXTAREA = "#content_parts"
    FILE_INPUT = "#uploaded_file"
    UPLOAD_BUTTON = "button[type='submit']:has-text('Upload Document')"
    SUCCESS_MESSAGE = ".message.success"
    ERROR_MESSAGE = ".message.error"

    # Selectors for document detail page
    DOCUMENT_TITLE = "h2"
    FULL_CONTENT_DISPLAY = ".full-content-display pre"
    METADATA_GRID = ".detail-grid"
    CHUNK_CARD = ".chunk-card"
    BACK_TO_SEARCH_LINK = "a:has-text('Back to Search')"

    async def navigate_to_documents_list(self) -> None:
        """Navigate to the documents list page."""
        await self.navigate_to("/documents/")
        await self.wait_for_load()

    async def navigate_to_upload_form(self) -> None:
        """Navigate to the document upload form."""
        await self.navigate_to("/documents/upload")
        await self.wait_for_load()

    async def navigate_to_document_detail(self, document_id: int) -> None:
        """Navigate to a specific document's detail page.

        Args:
            document_id: The ID of the document to view
        """
        await self.navigate_to(f"/vector-search/document/{document_id}")
        await self.wait_for_load()

    async def upload_document_with_file(
        self,
        source_type: str,
        source_id: str,
        source_uri: str,
        title: str,
        file_path: str,
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Upload a document with a file.

        Args:
            source_type: Type of the source (e.g., 'manual_upload')
            source_id: Unique ID within source type
            source_uri: Canonical URL/path
            title: Document title
            file_path: Path to the file to upload
            created_at: Optional ISO 8601 timestamp
            metadata: Optional metadata dictionary
        """
        await self.navigate_to_upload_form()

        # Fill required fields
        await self.fill_form_field(self.SOURCE_TYPE_INPUT, source_type)
        await self.fill_form_field(self.SOURCE_ID_INPUT, source_id)
        await self.fill_form_field(self.SOURCE_URI_INPUT, source_uri)
        await self.fill_form_field(self.TITLE_INPUT, title)

        # Fill optional fields
        if created_at:
            await self.fill_form_field(self.CREATED_AT_INPUT, created_at)

        if metadata:
            await self.fill_form_field(self.METADATA_TEXTAREA, json.dumps(metadata))

        # Handle file upload
        file_input = await self.page.wait_for_selector(self.FILE_INPUT)
        if file_input:
            await file_input.set_input_files(file_path)

        # Submit the form
        await self.page.click(self.UPLOAD_BUTTON)
        # Wait for either success or error message
        try:
            await self.page.wait_for_selector(
                f"{self.SUCCESS_MESSAGE}, {self.ERROR_MESSAGE}", timeout=5000
            )
        except Exception:
            # Fallback to checking for any response
            await self.page.wait_for_load_state("domcontentloaded")

    async def upload_document_with_content(
        self,
        source_type: str,
        source_id: str,
        source_uri: str,
        title: str,
        content_parts: dict[str, str],
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Upload a document with content parts instead of a file.

        Args:
            source_type: Type of the source
            source_id: Unique ID within source type
            source_uri: Canonical URL/path
            title: Document title
            content_parts: Dictionary of content parts
            created_at: Optional ISO 8601 timestamp
            metadata: Optional metadata dictionary
        """
        await self.navigate_to_upload_form()

        # Fill required fields
        await self.fill_form_field(self.SOURCE_TYPE_INPUT, source_type)
        await self.fill_form_field(self.SOURCE_ID_INPUT, source_id)
        await self.fill_form_field(self.SOURCE_URI_INPUT, source_uri)
        await self.fill_form_field(self.TITLE_INPUT, title)

        # Fill content parts
        await self.fill_form_field(
            self.CONTENT_PARTS_TEXTAREA, json.dumps(content_parts)
        )

        # Fill optional fields
        if created_at:
            await self.fill_form_field(self.CREATED_AT_INPUT, created_at)

        if metadata:
            await self.fill_form_field(self.METADATA_TEXTAREA, json.dumps(metadata))

        # Submit the form
        await self.page.click(self.UPLOAD_BUTTON)
        # Wait for either success or error message
        try:
            await self.page.wait_for_selector(
                f"{self.SUCCESS_MESSAGE}, {self.ERROR_MESSAGE}", timeout=5000
            )
        except Exception:
            # Fallback to checking for any response
            await self.page.wait_for_load_state("domcontentloaded")

    async def get_success_message(self) -> str | None:
        """Get the success message after upload.

        Returns:
            The success message text or None if not found
        """
        try:
            success_elem = await self.page.wait_for_selector(
                self.SUCCESS_MESSAGE, timeout=3000
            )
            if success_elem:
                return await success_elem.text_content()
        except Exception:
            pass
        return None

    async def get_error_message(self) -> str | None:
        """Get the error message after upload.

        Returns:
            The error message text or None if not found
        """
        try:
            error_elem = await self.page.wait_for_selector(
                self.ERROR_MESSAGE, timeout=3000
            )
            if error_elem:
                return await error_elem.text_content()
        except Exception:
            pass
        return None

    async def get_document_count(self) -> int:
        """Get the total count of documents from the list page.

        Returns:
            The total number of documents
        """
        await self.navigate_to_documents_list()
        try:
            count_elem = await self.page.wait_for_selector(self.TOTAL_COUNT_TEXT)
            if count_elem:
                text = await count_elem.text_content()
                if text:
                    # Extract number from "Total documents: X"
                    import re

                    match = re.search(r"Total documents:\s*(\d+)", text)
                    if match:
                        return int(match.group(1))
        except Exception:
            pass
        return 0

    async def get_document_rows(self) -> list[dict[str, Any]]:
        """Get all document rows from the current page.

        Returns:
            List of dictionaries with document information
        """
        rows = await self.page.query_selector_all(self.DOCUMENT_ROW)
        documents = []

        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) >= 6:  # Expected number of columns
                title_text = await cells[0].text_content()
                type_text = await cells[1].text_content()
                source_id_text = await cells[2].text_content()
                created_text = await cells[3].text_content()
                added_text = await cells[4].text_content()

                # Get document ID from View link
                view_link = await cells[5].query_selector("a:has-text('View')")
                doc_id = None
                if view_link:
                    href = await view_link.get_attribute("href")
                    if href:
                        # Extract ID from URL like /vector-search/document/123
                        import re

                        match = re.search(r"/document/(\d+)", href)
                        if match:
                            doc_id = int(match.group(1))

                documents.append({
                    "title": title_text.strip() if title_text else None,
                    "type": type_text.strip() if type_text else None,
                    "source_id": source_id_text.strip() if source_id_text else None,
                    "created": created_text.strip() if created_text else None,
                    "added": added_text.strip() if added_text else None,
                    "id": doc_id,
                })

        return documents

    async def is_document_present(self, title: str) -> bool:
        """Check if a document with the given title is present in the list.

        Args:
            title: The title to search for

        Returns:
            True if the document is found, False otherwise
        """
        documents = await self.get_document_rows()
        return any(doc["title"] == title for doc in documents)

    async def click_view_document(self, title: str) -> None:
        """Click the View link for a document with the given title.

        Args:
            title: The title of the document to view
        """
        await self.navigate_to_documents_list()
        # Find the row containing the title
        rows = await self.page.query_selector_all(self.DOCUMENT_ROW)
        for row in rows:
            title_cell = await row.query_selector("td:first-child")
            if title_cell:
                cell_text = await title_cell.text_content()
                if cell_text and title in cell_text:
                    # Click the View link in this row
                    view_link = await row.query_selector("a:has-text('View')")
                    if view_link:
                        await view_link.click()
                        await self.wait_for_load()
                        return

        raise ValueError(f"Document with title '{title}' not found")

    async def reindex_document(self, title: str) -> None:
        """Click the Re-index button for a document with the given title.

        Args:
            title: The title of the document to reindex
        """
        await self.navigate_to_documents_list()
        # Find the row containing the title
        rows = await self.page.query_selector_all(self.DOCUMENT_ROW)
        for row in rows:
            title_cell = await row.query_selector("td:first-child")
            if title_cell:
                cell_text = await title_cell.text_content()
                if cell_text and title in cell_text:
                    # Click the Re-index button in this row
                    reindex_button = await row.query_selector(self.REINDEX_BUTTON)
                    if reindex_button:
                        await reindex_button.click()
                        await self.wait_for_load()
                        return

        raise ValueError(f"Document with title '{title}' not found")

    async def is_empty_state_visible(self) -> bool:
        """Check if the empty state message is visible.

        Returns:
            True if the empty state is shown, False otherwise
        """
        return await self.is_element_visible(self.NO_DOCUMENTS_MESSAGE)

    async def get_document_detail_title(self) -> str | None:
        """Get the title from the document detail page.

        Returns:
            The document title or None if not found
        """
        title_elem = await self.page.wait_for_selector(self.DOCUMENT_TITLE)
        if title_elem:
            text = await title_elem.text_content()
            if text:
                # Extract title from "Document: Title (ID: 123)"
                import re

                match = re.match(r"Document:\s*(.+?)\s*\(ID:", text)
                if match:
                    return match.group(1)
                # If no ID part, just remove "Document: " prefix
                if text.startswith("Document: "):
                    return text[10:]
        return None

    async def get_document_full_content(self) -> str | None:
        """Get the full content from the document detail page.

        Returns:
            The document content or None if not found
        """
        try:
            content_elem = await self.page.wait_for_selector(
                self.FULL_CONTENT_DISPLAY, timeout=3000
            )
            if content_elem:
                return await content_elem.text_content()
        except Exception:
            pass
        return None

    async def get_chunk_count(self) -> int:
        """Get the number of content chunks on the document detail page.

        Returns:
            The number of chunks
        """
        chunks = await self.page.query_selector_all(self.CHUNK_CARD)
        return len(chunks)

    async def has_pagination(self) -> bool:
        """Check if pagination controls are visible.

        Returns:
            True if pagination is present, False otherwise
        """
        prev_visible = await self.is_element_visible(self.PAGINATION_PREV)
        next_visible = await self.is_element_visible(self.PAGINATION_NEXT)
        return prev_visible or next_visible

    async def go_to_next_page(self) -> bool:
        """Navigate to the next page of documents.

        Returns:
            True if navigation was successful, False if no next page
        """
        if await self.is_element_visible(self.PAGINATION_NEXT):
            await self.page.click(self.PAGINATION_NEXT)
            await self.wait_for_load()
            return True
        return False

    async def wait_for_upload_complete(self, timeout: int = 5000) -> bool:
        """Wait for document upload to complete.

        Args:
            timeout: Maximum time to wait in milliseconds

        Returns:
            True if upload completed (success or error), False if timeout
        """
        try:
            # Wait for either success or error message to appear
            # The form stays on the same page and shows the message
            await self.page.wait_for_selector(
                f"{self.SUCCESS_MESSAGE}, {self.ERROR_MESSAGE}",
                state="visible",
                timeout=timeout,
            )
            return True
        except Exception as e:
            print(f"Wait for upload complete failed: {e}")
            # Try a more general selector
            any_message = await self.page.query_selector(".message")
            if any_message:
                print("Found a message element but not matching our selectors")
                text = await any_message.text_content()
                print(f"Message text: {text}")
            return False
