"""Test React Documents UI functionality using Playwright."""

import json
import uuid

import httpx
import pytest
from playwright.async_api import expect

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

    # Verify page has loaded by checking for key elements
    await page.wait_for_selector("body", timeout=10000)

    # The React app should render some content
    content = await page.text_content("body")
    assert content is not None and len(content.strip()) > 0, (
        "Page content should not be empty"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_create_document_via_api_and_view_in_react_ui(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test creating a document via API and viewing it in the React UI."""
    page = web_test_fixture.page

    # Step 1: Create a document via the API
    # Generate unique source ID to avoid conflicts
    test_doc_source_id = f"test-react-ui-{uuid.uuid4()}"

    # Create HTTP client using the same base URL as the web test
    async with httpx.AsyncClient(base_url=web_test_fixture.base_url) as client:
        api_form_data = {
            "source_type": TEST_DOC_SOURCE_TYPE,
            "source_id": test_doc_source_id,
            "title": TEST_DOC_TITLE,
            "metadata": TEST_DOC_METADATA_JSON,
            "content_parts": TEST_DOC_CONTENT_PARTS_JSON,
            "source_uri": "",
        }

        response = await client.post("/api/documents/upload", data=api_form_data)

        # Verify the document was created successfully
        assert response.status_code == 202, (
            f"API call failed: {response.status_code} - {response.text}"
        )
        response_data = response.json()
        assert "document_id" in response_data
        assert response_data.get("task_enqueued") is True
        document_db_id = response_data["document_id"]

    # Step 2: Navigate to the React Documents UI and verify the document appears
    await page.goto(f"{web_test_fixture.base_url}/documents")

    # Wait for the page to load
    await page.wait_for_selector("body", timeout=10000)

    # Wait for the React component to load and fetch data
    # Look for the "Total documents:" text which indicates the API call has completed
    await page.wait_for_selector("text=Total documents:", timeout=15000)

    # Give additional time for documents to appear (background indexing might be needed)
    max_retries = 10
    document_found = False

    for _attempt in range(max_retries):
        page_content = await page.text_content("body")
        assert page_content is not None, "Page content should not be None"

        if TEST_DOC_TITLE in page_content:
            document_found = True
            break

        # Wait before retrying
        await page.wait_for_timeout(1000)
        # Refresh the page to reload documents
        await page.reload()
        await page.wait_for_selector("text=Total documents:", timeout=10000)

    assert document_found, (
        f"Document title '{TEST_DOC_TITLE}' should appear on the documents page after {max_retries} retries"
    )

    # Step 3: Verify the document has a clickable link
    # The React UI currently links documents to /vector-search?document_id=X
    # We'll just verify the link exists without clicking it to avoid navigation issues

    # Find the document title link
    title_link = page.locator(f"a:has-text('{TEST_DOC_TITLE}')")

    assert await title_link.count() > 0, (
        f"Could not find link for document '{TEST_DOC_TITLE}'"
    )

    # Verify the link has the correct href attribute
    href = await title_link.get_attribute("href")
    assert href is not None, "Document link should have an href attribute"
    assert "/vector-search" in href, (
        f"Document link should point to vector-search, got: {href}"
    )
    assert f"document_id={document_db_id}" in href, "Document ID should be in link URL"

    # Step 4: Verify document metadata is displayed
    # Check that other document fields are visible in the table
    page_content = await page.text_content("body")
    assert page_content is not None, "Page content should not be None"

    # Verify document source type is displayed
    assert TEST_DOC_SOURCE_TYPE in page_content, (
        "Document source type should be visible"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_documents_display_in_react_ui(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that multiple documents are displayed properly in the React UI."""
    page = web_test_fixture.page

    # Create two test documents via API
    documents_created = []

    async with httpx.AsyncClient(base_url=web_test_fixture.base_url) as client:
        for i in range(2):
            test_doc_source_id = f"test-react-multi-{i}-{uuid.uuid4()}"
            title = f"Test Document {i + 1}"

            api_form_data = {
                "source_type": TEST_DOC_SOURCE_TYPE,
                "source_id": test_doc_source_id,
                "title": title,
                "metadata": json.dumps({"test_index": i}),
                "content_parts": json.dumps({
                    "title": title,
                    "content": f"This is test document number {i + 1} content.",
                }),
                "source_uri": "",
            }

            response = await client.post("/api/documents/upload", data=api_form_data)
            assert response.status_code == 202
            response_data = response.json()
            documents_created.append({
                "id": response_data["document_id"],
                "title": title,
            })

    # Navigate to documents page and verify both documents appear
    await page.goto(f"{web_test_fixture.base_url}/documents")
    await page.wait_for_selector("body", timeout=10000)

    # Wait for the React component to load and fetch data
    await page.wait_for_selector("text=Total documents:", timeout=15000)

    # Verify both document titles appear on the page with retry logic
    max_retries = 10
    all_found = False

    for _attempt in range(max_retries):
        page_content = await page.text_content("body")
        assert page_content is not None, "Page content should not be None"

        found_count = sum(
            1 for doc in documents_created if doc["title"] in page_content
        )
        if found_count == len(documents_created):
            all_found = True
            break

        # Wait before retrying
        await page.wait_for_timeout(1000)
        # Refresh the page to reload documents
        await page.reload()
        await page.wait_for_selector("text=Total documents:", timeout=10000)

    # Final verification
    assert all_found, (
        f"All {len(documents_created)} document titles should appear on the documents page after {max_retries} retries"
    )
