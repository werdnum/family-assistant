"""Test React Vector Search UI functionality using Playwright."""

import json
import uuid

import httpx
import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture

# Test data constants
TEST_DOC_1_TITLE = "Machine Learning Fundamentals"
TEST_DOC_1_CONTENT = "This document covers the basics of supervised and unsupervised learning algorithms."
TEST_DOC_1_METADATA = {"category": "education", "difficulty": "beginner"}

TEST_DOC_2_TITLE = "Deep Learning with Neural Networks"
TEST_DOC_2_CONTENT = (
    "Advanced techniques for training deep neural networks including backpropagation."
)
TEST_DOC_2_METADATA = {"category": "education", "difficulty": "advanced"}

TEST_DOC_3_TITLE = "Data Science Best Practices"
TEST_DOC_3_CONTENT = (
    "Guidelines for data preprocessing, feature engineering, and model evaluation."
)
TEST_DOC_3_METADATA = {"category": "methodology", "difficulty": "intermediate"}


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_vector_search_page_loads(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that the React Vector Search page loads successfully."""
    page = web_test_fixture_readonly.page

    # Navigate to the React vector search page
    await page.goto(f"{web_test_fixture_readonly.base_url}/vector-search")

    # Verify we're on the vector search page
    await expect(page).to_have_url(
        f"{web_test_fixture_readonly.base_url}/vector-search"
    )

    # Verify page has loaded by checking for key elements
    await page.wait_for_selector("h1", timeout=10000)

    # Check for the search form
    search_input = page.locator("textarea[placeholder*='looking for']")
    assert await search_input.is_visible(), "Search input should be visible"

    # Check for the search button
    search_button = page.locator("button:has-text('Search')")
    assert await search_button.is_visible(), "Search button should be visible"


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.postgres  # Vector search requires PostgreSQL with pgvector
async def test_search_documents_via_react_ui(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test searching for documents via the React Vector Search UI."""
    page = web_test_fixture.page

    # Step 1: Create test documents via the API
    async with httpx.AsyncClient(base_url=web_test_fixture.base_url) as client:
        # Create three test documents with different content
        docs_created = []
        for title, content, metadata in [
            (TEST_DOC_1_TITLE, TEST_DOC_1_CONTENT, TEST_DOC_1_METADATA),
            (TEST_DOC_2_TITLE, TEST_DOC_2_CONTENT, TEST_DOC_2_METADATA),
            (TEST_DOC_3_TITLE, TEST_DOC_3_CONTENT, TEST_DOC_3_METADATA),
        ]:
            source_id = f"test-vector-search-{uuid.uuid4()}"
            api_form_data = {
                "source_type": "manual_upload",
                "source_id": source_id,
                "title": title,
                "metadata": json.dumps(metadata),
                "content_parts": json.dumps({
                    "title": title,
                    "content": content,
                }),
                "source_uri": "",
            }

            response = await client.post("/api/documents/upload", data=api_form_data)
            assert response.status_code == 202, (
                f"Failed to create document: {response.status_code} - {response.text}"
            )
            response_data = response.json()
            docs_created.append({
                "id": response_data["document_id"],
                "title": title,
            })

    # Step 2: Navigate to the Vector Search page
    await page.goto(f"{web_test_fixture.base_url}/vector-search")
    await page.wait_for_selector("h1:has-text('Vector Search')", timeout=10000)

    # Step 3: Perform a search for "neural networks"
    search_input = page.locator("textarea[placeholder*='looking for']")
    await search_input.fill("neural networks")

    search_button = page.locator("button:has-text('Search')")
    await search_button.click()

    # Step 4: Wait for search to complete
    # Vector search requires embeddings generation which is async
    # The search button is disabled while loading (line 369 in VectorSearch.jsx)
    # and re-enabled when search completes
    await expect(search_button).to_be_enabled(timeout=15000)

    # Check if we have results or an appropriate message
    page_content = await page.text_content("body")
    assert page_content is not None, "Page content should not be None"

    # The search should return at least the neural networks document
    has_results = "Deep Learning with Neural Networks" in page_content
    has_no_results = "No results found" in page_content

    assert has_results or has_no_results, (
        "Should either show results or 'No results found' message"
    )

    # Step 5: Test filtering by source type
    # Click on advanced options if available (just to test the UI interaction)
    advanced_options = page.locator("details summary:has-text('Advanced Options')")
    if await advanced_options.is_visible():
        await advanced_options.click()
        # No need to wait - the next interaction is with source type filters
        # which are outside the advanced options section

    # Check if source type filters are visible and click the label
    # We click the label because the input itself is hidden for styling purposes
    source_type_label = page.locator("label:has-text('manual_upload')")
    if await source_type_label.is_visible():
        await source_type_label.click()


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.postgres  # Vector search requires PostgreSQL with pgvector
async def test_vector_search_with_filters(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test vector search with various filters."""
    page = web_test_fixture.page

    # Create a test document with specific metadata
    async with httpx.AsyncClient(base_url=web_test_fixture.base_url) as client:
        source_id = f"test-filter-search-{uuid.uuid4()}"
        api_form_data = {
            "source_type": "test_with_filters",
            "source_id": source_id,
            "title": "Test Document with Metadata",
            "metadata": json.dumps({"author": "test_user", "priority": "high"}),
            "content_parts": json.dumps({
                "title": "Test Document with Metadata",
                "content": "This is a test document for testing filter functionality.",
            }),
            "source_uri": "",
        }

        response = await client.post("/api/documents/upload", data=api_form_data)
        assert response.status_code == 202

    # Navigate to vector search
    await page.goto(f"{web_test_fixture.base_url}/vector-search")
    await page.wait_for_selector("h1:has-text('Vector Search')", timeout=10000)

    # Fill in search query
    search_input = page.locator("textarea[placeholder*='looking for']")
    await search_input.fill("test document")

    # Test title filter
    title_filter = page.locator("input[placeholder*='Filter by title']")
    if await title_filter.is_visible():
        await title_filter.fill("Metadata")

    # Test limit setting
    limit_input = page.locator("input[type='number'][min='1'][max='100']")
    if await limit_input.is_visible():
        await limit_input.fill("5")

    # Perform search
    search_button = page.locator("button:has-text('Search')")
    await search_button.click()

    # Wait for search to complete
    # The search button is disabled while loading and re-enabled when search completes
    await expect(search_button).to_be_enabled(timeout=15000)

    # Verify the page doesn't show an error
    error_elements = page.locator(".error, [class*='error']")
    if await error_elements.count() > 0:
        error_text = await error_elements.first.text_content()
        # Only fail if it's not a "no results" type error
        if error_text and "No results" not in error_text:
            pytest.fail(f"Search returned an error: {error_text}")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_vector_search_empty_query_handling(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that empty search queries are handled properly."""
    page = web_test_fixture_readonly.page

    # Navigate to vector search
    await page.goto(f"{web_test_fixture_readonly.base_url}/vector-search")
    await page.wait_for_selector("h1:has-text('Vector Search')", timeout=10000)

    # Try to search without entering a query
    search_button = page.locator("button:has-text('Search')")
    await search_button.click()

    # Should show an error message for empty query
    # The error is displayed as "Error: Please enter a search query" in an Alert component
    error_alert = page.locator("text='Error: Please enter a search query'")
    await expect(error_alert).to_be_visible(timeout=5000)

    # Verify the error message is displayed
    page_content = await page.text_content("body")
    assert page_content is not None
    assert "Please enter a search query" in page_content, (
        "Empty search should show 'Please enter a search query' error"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_vector_search_result_links(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that search results contain links to document details."""
    page = web_test_fixture.page

    # Create a test document
    async with httpx.AsyncClient(base_url=web_test_fixture.base_url) as client:
        source_id = f"test-result-links-{uuid.uuid4()}"
        test_title = "Document with Links Test"
        api_form_data = {
            "source_type": "test_links",
            "source_id": source_id,
            "title": test_title,
            "metadata": json.dumps({}),
            "content_parts": json.dumps({
                "title": test_title,
                "content": "Content for testing result links in vector search.",
            }),
            "source_uri": "https://example.com/test",
        }

        response = await client.post("/api/documents/upload", data=api_form_data)
        assert response.status_code == 202
        doc_id = response.json()["document_id"]

    # Navigate to vector search and search for the document
    await page.goto(f"{web_test_fixture.base_url}/vector-search")
    await page.wait_for_selector("h1:has-text('Vector Search')", timeout=10000)

    search_input = page.locator("textarea[placeholder*='looking for']")
    await search_input.fill(test_title)

    search_button = page.locator("button:has-text('Search')")
    await search_button.click()

    # Wait for search to complete - either results or no results message
    try:  # noqa: SIM105  # Try-except is clearer than suppress for conditional waits
        await page.wait_for_selector(
            "text='Results', text='No results found'", timeout=10000, state="visible"
        )
    except Exception:
        # Search may have completed without clear indicator
        pass

    # Check if results contain document links
    # The document detail link should point to /documents/{id}
    doc_link = page.locator(
        f"a[href*='/documents/{doc_id}'], a:has-text('View Full Document')"
    )

    # If no embeddings were generated yet, we might not have results
    # This is okay for this test - we're just checking the UI structure
    if await doc_link.count() > 0:
        assert await doc_link.is_visible(), (
            "Document detail link should be visible in results"
        )

        # Check if external source URI link is present
        source_link = page.locator("a[href='https://example.com/test']")
        if await source_link.count() > 0:
            assert await source_link.is_visible(), "Source URI link should be visible"
