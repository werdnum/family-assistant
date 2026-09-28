"""Test React Vector Search UI functionality using Playwright."""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from playwright.async_api import expect
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.storage.database import Database
from tests.functional.web.conftest import WebTestFixture
from tests.helpers import wait_for_tasks_to_complete

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


async def _add_search_embedding(
    db_engine: AsyncEngine,
    document_id: int,
    content: str,
    embedding_generator: EmbeddingGenerator,
) -> None:
    """Make an uploaded document searchable with the web fixture's empty indexing pipeline."""
    embedding_result = await embedding_generator.generate_embeddings([content])
    db = Database(engine=db_engine)
    await db.vector.add_embedding(
        document_id=document_id,
        chunk_index=0,
        embedding_type="content_chunk",
        embedding=embedding_result.embeddings[0],
        embedding_model=embedding_result.model_name,
        content=content,
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_react_vector_search_page_loads(
    web_test_fixture_readonly: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
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

    # Take screenshot
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "vector-search-empty", viewport)

    assert await search_button.is_visible(), "Search button should be visible"


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.postgres  # Vector search requires PostgreSQL with pgvector
async def test_search_documents_via_react_ui(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """Test searching for documents via the React Vector Search UI."""
    page = web_test_fixture.page

    # Step 1: Create test documents via the API
    async with httpx.AsyncClient(base_url=web_test_fixture.base_url) as client:
        # Create three test documents with different content
        document_ids = []
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
            document_ids.append(response.json()["document_id"])
    await wait_for_tasks_to_complete(db_engine, task_ids=None, timeout_seconds=60.0)
    embedding_generator = web_test_fixture.assistant.embedding_generator
    assert embedding_generator is not None
    for document_id, content in zip(
        document_ids,
        ("supervised", "neural networks", "data preprocessing"),
        strict=True,
    ):
        await _add_search_embedding(
            db_engine, document_id, content, embedding_generator
        )

    # Step 2: Navigate to the Vector Search page
    await page.goto(f"{web_test_fixture.base_url}/vector-search")
    await page.wait_for_selector("h1:has-text('Vector Search')", timeout=10000)

    # Step 3: Perform a search for "neural networks"
    search_input = page.locator("textarea[placeholder*='looking for']")
    await search_input.fill("neural networks")

    search_button = page.locator("button:has-text('Search')")
    async with page.expect_response("**/api/vector-search/") as search_response_info:
        await search_button.click()
    search_response = await search_response_info.value
    assert search_response.status == 200, await search_response.text()

    await expect(
        page.locator("article").first.get_by_role("heading", name=TEST_DOC_2_TITLE)
    ).to_be_visible(timeout=15000)


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.postgres  # Vector search requires PostgreSQL with pgvector
async def test_vector_search_with_filters(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
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
        matching_document_id = response.json()["document_id"]

        response = await client.post(
            "/api/documents/upload",
            data={
                **api_form_data,
                "source_id": f"test-filter-search-{uuid.uuid4()}",
                "title": "Test Document without Match",
                "content_parts": json.dumps({
                    "title": "Test Document without Match",
                    "content": "This is another test document for testing filter functionality.",
                }),
            },
        )
        assert response.status_code == 202
        other_document_id = response.json()["document_id"]

    await wait_for_tasks_to_complete(db_engine, task_ids=None, timeout_seconds=60.0)
    embedding_generator = web_test_fixture.assistant.embedding_generator
    assert embedding_generator is not None
    await _add_search_embedding(
        db_engine,
        matching_document_id,
        "This is a test document for testing filter functionality.",
        embedding_generator,
    )
    await _add_search_embedding(
        db_engine,
        other_document_id,
        "This is another test document for testing filter functionality.",
        embedding_generator,
    )

    # Navigate to vector search
    await page.goto(f"{web_test_fixture.base_url}/vector-search")
    await page.wait_for_selector("h1:has-text('Vector Search')", timeout=10000)

    # Fill in search query
    search_input = page.locator("textarea[placeholder*='looking for']")
    await search_input.fill("test document")

    # Test title filter
    title_filter = page.get_by_placeholder("Filter by title...")
    await title_filter.fill("Metadata")

    # Perform search
    search_button = page.locator("button:has-text('Search')")
    await search_button.click()

    await expect(page.locator("article")).to_have_count(1, timeout=15000)
    await expect(
        page.locator("article").get_by_role(
            "heading", name="Test Document with Metadata"
        )
    ).to_be_visible()


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
@pytest.mark.postgres  # Vector search requires PostgreSQL with pgvector
async def test_vector_search_result_links(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
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

    await wait_for_tasks_to_complete(db_engine, task_ids=None, timeout_seconds=60.0)
    embedding_generator = web_test_fixture.assistant.embedding_generator
    assert embedding_generator is not None
    await _add_search_embedding(
        db_engine,
        doc_id,
        "Content for testing result links in vector search.",
        embedding_generator,
    )

    # Navigate to vector search and search for the document
    await page.goto(f"{web_test_fixture.base_url}/vector-search")
    await page.wait_for_selector("h1:has-text('Vector Search')", timeout=10000)

    search_input = page.locator("textarea[placeholder*='looking for']")
    await search_input.fill(test_title)

    search_button = page.locator("button:has-text('Search')")
    await search_button.click()

    await expect(page.locator(f"article a[href='/documents/{doc_id}']")).to_be_visible(
        timeout=15000
    )
    await expect(
        page.locator("article a[href='https://example.com/test']")
    ).to_be_visible()
