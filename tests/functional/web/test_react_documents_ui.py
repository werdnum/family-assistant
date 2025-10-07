"""Test React Documents UI functionality using Playwright."""

import json
import uuid

import httpx
import pytest
from playwright.async_api import expect
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.helpers import wait_for_tasks_to_complete

from .conftest import WebTestFixture

# Test data constants
TEST_DOC_SOURCE_TYPE = "manual_test_upload"
TEST_DOC_TITLE = "Test React UI Document"
TEST_DOC_CHUNK_0 = "This is a test document created via API for React UI testing."
TEST_DOC_CHUNK_1 = "It contains multiple content chunks to verify proper display."
TEST_DOC_METADATA = {"author": "test_user", "test": True}

# Content parts dictionary for the document
TEST_DOC_CONTENT_PARTS = {
    "title": TEST_DOC_TITLE,
    "content_chunk_0": TEST_DOC_CHUNK_0,
    "content_chunk_1": TEST_DOC_CHUNK_1,
}

# Convert to JSON strings for API
TEST_DOC_CONTENT_PARTS_JSON = json.dumps(TEST_DOC_CONTENT_PARTS)
TEST_DOC_METADATA_JSON = json.dumps(TEST_DOC_METADATA)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_documents_page_loads(web_test_fixture: WebTestFixture) -> None:
    """Test that the React Documents page loads successfully."""
    page = web_test_fixture.page

    # Navigate to the React documents page
    await page.goto(f"{web_test_fixture.base_url}/documents")

    # Verify we're on the documents page
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/documents")

    # Wait for React app to mount - check for our custom attribute
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Wait for actual content to render (not just React mounting)
    await page.wait_for_function(
        """() => {
            const body = document.body;
            return body && body.textContent && body.textContent.trim().length > 20;
        }""",
        timeout=10000,
    )

    # The React app should render some content
    content = await page.text_content("body")
    assert content is not None and len(content.strip()) > 0, (
        "Page content should not be empty"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_create_document_via_api_and_view_in_react_ui(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """Test creating a document via API and viewing it in the React UI."""
    page = web_test_fixture.page

    # Create a document via API using the upload endpoint with form data
    source_id = str(uuid.uuid4())
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{web_test_fixture.base_url}/api/documents/upload",
            data={
                "source_id": source_id,
                "source_type": TEST_DOC_SOURCE_TYPE,
                "source_uri": f"test://document/{source_id}",
                "title": TEST_DOC_TITLE,
                "content_parts": TEST_DOC_CONTENT_PARTS_JSON,
                "metadata": TEST_DOC_METADATA_JSON,
            },
        )
        assert response.status_code in {200, 202}, (
            f"Failed to create document: {response.text}"
        )

        # Wait for background document processing task to complete
        await wait_for_tasks_to_complete(
            db_engine,
            task_types={"process_uploaded_document"},
            timeout_seconds=20.0,
        )

        # Get the actual document ID from the database
        # The upload creates the document synchronously before returning
        response = await client.get(f"{web_test_fixture.base_url}/api/documents/")
        docs = response.json()["documents"]
        # Find our document by source_id
        doc = next((d for d in docs if d["source_id"] == source_id), None)
        assert doc is not None, f"Document with source_id {source_id} not found"
        doc_id = doc["id"]

    # Navigate to the React documents page
    await page.goto(f"{web_test_fixture.base_url}/documents")

    # Wait for the React app to load
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Wait for content
    await page.wait_for_function(
        """() => {
            const body = document.body;
            return body && body.textContent && body.textContent.trim().length > 20;
        }""",
        timeout=10000,
    )

    # Look for the document title in the list
    doc_link = page.locator(f"text={TEST_DOC_TITLE}")
    await expect(doc_link).to_be_visible(timeout=10000)

    # Click on the document to view details
    await doc_link.click()

    # Should navigate to document detail page
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/documents/{doc_id}")

    # Wait for the document title to be visible on the detail page
    # This ensures the document details have loaded
    await expect(page.locator(f"text={TEST_DOC_TITLE}")).to_be_visible(timeout=10000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_documents_display_in_react_ui(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """Test that multiple documents are displayed correctly in the React UI."""
    page = web_test_fixture.page

    # Create multiple documents via API
    doc_ids = []
    for i in range(3):
        doc_id = str(uuid.uuid4())
        doc_title = f"Test Document {i + 1}"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{web_test_fixture.base_url}/api/documents/upload",
                data={
                    "source_id": doc_id,
                    "source_type": TEST_DOC_SOURCE_TYPE,
                    "source_uri": f"test://document/{doc_id}",
                    "title": doc_title,
                    "content_parts": json.dumps({
                        "title": doc_title,
                        "content": f"This is test document number {i + 1}",
                    }),
                    "metadata": json.dumps({"index": i}),
                },
            )
            assert response.status_code in {200, 202}
            doc_ids.append(doc_id)

    # Wait for all background document processing tasks to complete
    await wait_for_tasks_to_complete(
        db_engine,
        task_types={"process_uploaded_document"},
        timeout_seconds=20.0,
    )

    # Navigate to the React documents page
    await page.goto(f"{web_test_fixture.base_url}/documents")

    # Wait for the React app to load
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Wait for content
    await page.wait_for_function(
        """() => {
            const body = document.body;
            return body && body.textContent && body.textContent.trim().length > 20;
        }""",
        timeout=10000,
    )

    # Check that all documents are visible
    for i in range(3):
        doc_title = f"Test Document {i + 1}"
        await expect(page.locator(f"text={doc_title}")).to_be_visible(timeout=10000)

    # Verify we can see multiple document entries
    doc_links = page.locator("a[href^='/documents/']")
    count = await doc_links.count()
    assert count >= 3, f"Should have at least 3 document links, found {count}"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_document_search_in_react_ui(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """Test searching for documents in the React UI."""
    page = web_test_fixture.page

    # Create documents with different titles
    doc_titles = ["Python Tutorial", "JavaScript Guide", "Python Reference"]
    for title in doc_titles:
        doc_id = str(uuid.uuid4())
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{web_test_fixture.base_url}/api/documents/upload",
                data={
                    "source_id": doc_id,
                    "source_type": TEST_DOC_SOURCE_TYPE,
                    "source_uri": f"test://document/{doc_id}",
                    "title": title,
                    "content_parts": json.dumps({
                        "title": title,
                        "content": f"Content for {title}",
                    }),
                    "metadata": json.dumps({}),
                },
            )

    # Wait for all background document processing tasks to complete
    await wait_for_tasks_to_complete(
        db_engine,
        task_types={"process_uploaded_document"},
        timeout_seconds=20.0,
    )

    # Navigate to the React documents page
    await page.goto(f"{web_test_fixture.base_url}/documents")

    # Wait for the React app to load
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Wait for content
    await page.wait_for_function(
        """() => {
            const body = document.body;
            return body && body.textContent && body.textContent.trim().length > 20;
        }""",
        timeout=10000,
    )

    # Look for search input
    search_input = page.locator("input[type='text'], input[type='search']").first
    if await search_input.is_visible():
        # Type search query
        await search_input.fill("Python")

        # Wait for filtered results - check that Python documents appear
        await expect(page.locator("text=Python Tutorial")).to_be_visible(timeout=5000)
        await expect(page.locator("text=Python Reference")).to_be_visible(timeout=5000)

        # JavaScript Guide should be hidden or not present
        js_guide = page.locator("text=JavaScript Guide")
        is_visible = await js_guide.is_visible()
        assert not is_visible, (
            "JavaScript Guide should not be visible when searching for Python"
        )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_document_detail_navigation_in_react_ui(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """Test navigating to document details and back in the React UI."""
    page = web_test_fixture.page

    # Create a document
    source_id = str(uuid.uuid4())
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{web_test_fixture.base_url}/api/documents/upload",
            data={
                "source_id": source_id,
                "source_type": TEST_DOC_SOURCE_TYPE,
                "source_uri": f"test://document/{source_id}",
                "title": TEST_DOC_TITLE,
                "content_parts": TEST_DOC_CONTENT_PARTS_JSON,
                "metadata": TEST_DOC_METADATA_JSON,
            },
        )
        assert response.status_code in {200, 202}, (
            f"Failed to create document: {response.text}"
        )

        # Wait for background document processing task to complete
        await wait_for_tasks_to_complete(
            db_engine,
            task_types={"process_uploaded_document"},
            timeout_seconds=20.0,
        )

        # Get the actual document ID from the database
        response = await client.get(f"{web_test_fixture.base_url}/api/documents/")
        docs = response.json()["documents"]
        doc = next((d for d in docs if d["source_id"] == source_id), None)
        assert doc is not None, f"Document with source_id {source_id} not found"
        doc_id = doc["id"]

    # Navigate to documents list
    await page.goto(f"{web_test_fixture.base_url}/documents")

    # Wait for the React app to load
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Wait for content
    await page.wait_for_function(
        """() => {
            const body = document.body;
            return body && body.textContent && body.textContent.trim().length > 20;
        }""",
        timeout=10000,
    )

    # Click on the document
    await page.locator(f"text={TEST_DOC_TITLE}").click()

    # Should navigate to detail page
    await expect(page).to_have_url(f"{web_test_fixture.base_url}/documents/{doc_id}")

    # Look for back button or link
    back_buttons = await page.locator("button:has-text('Back')").all()
    back_links = await page.locator("a:has-text('Back')").all()

    if back_buttons:
        # Click the first back button found
        await back_buttons[0].click()
    elif back_links:
        # Click the first back link found
        await back_links[0].click()

    if back_buttons or back_links:
        # Should navigate back to documents list
        await page.wait_for_function(
            """() => {
                const body = document.body;
                return body && body.textContent && body.textContent.trim().length > 20;
            }""",
            timeout=10000,
        )
        assert "/documents" in page.url, "Should navigate back to documents list"
