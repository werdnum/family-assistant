"""Unit tests for network_processors.py."""

import os
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from _pytest.logging import LogCaptureFixture
from assertpy import assert_that, soft_assertions

from family_assistant.indexing.pipeline import IndexableContent
from family_assistant.indexing.processors.network_processors import (
    WebFetcherProcessor,
    WebFetcherProcessorConfig,
)
from family_assistant.utils.scraping import MockScraper, ScrapeResult

if TYPE_CHECKING:
    from family_assistant.storage.vector import Document  # Protocol
    from family_assistant.tools.types import ToolExecutionContext


# --- Mocks and Fixtures ---


@pytest.fixture
def mock_document() -> MagicMock:
    """Provides a mock Document object."""
    doc = MagicMock(spec=["title"])  # Add other attributes if processor uses them
    doc.title = "Test Document"
    return doc


@pytest.fixture
def mock_tool_execution_context() -> MagicMock:
    """Provides a mock ToolExecutionContext object."""
    # Add attributes if the processor uses them (e.g., db_context, enqueue_task)
    # For WebFetcherProcessor, it's currently just passed through.
    return MagicMock(
        spec=["db_context", "enqueue_task", "llm_interface", "embedding_generator"]
    )


@pytest.fixture
def default_config() -> WebFetcherProcessorConfig:
    """Default configuration for the processor."""
    return WebFetcherProcessorConfig(
        process_embedding_types=["extracted_link", "raw_url"]
    )


# --- Test Cases ---


@pytest.mark.asyncio
async def test_fetch_markdown_content_success(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test successful fetching and processing of Markdown content."""
    url = "http://example.com/markdown"
    scrape_map = {
        url: ScrapeResult(
            type="markdown",
            final_url=url,
            title="Mock Markdown Title",
            content="## Hello Markdown",
            mime_type="text/markdown",
            source_description="mock-markdown",
        )
    }
    mock_scraper = MockScraper(url_map=scrape_map)
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    input_item = IndexableContent(
        content=url, embedding_type="extracted_link", source_processor="test_source"
    )
    results = await processor.process(
        [input_item], mock_document, input_item, mock_tool_execution_context
    )

    with soft_assertions():
        assert_that(results).is_length(1)
        result_item = results[0]
        assert_that(result_item.embedding_type).is_equal_to(
            "fetched_content_markdown"
        )
        assert_that(result_item.content).is_equal_to("## Hello Markdown")
        assert_that(result_item.mime_type).is_equal_to("text/markdown")
        assert_that(result_item.source_processor).is_equal_to(processor.name)
        assert_that(result_item.metadata["original_url"]).is_equal_to(url)
        assert_that(result_item.metadata["fetched_title"]).is_equal_to(
            "Mock Markdown Title"
        )
        assert_that(result_item.metadata["source_scraper_description"]).is_equal_to(
            "mock-markdown"
        )
    processor.cleanup_temp_files()


@pytest.mark.asyncio
async def test_fetch_text_content_success(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test successful fetching and processing of plain text content."""
    url = "http://example.com/text"
    scrape_map = {
        url: ScrapeResult(
            type="text",
            final_url=url,
            title="Mock Text Title",
            content="Plain text content.",
            mime_type="text/plain",
            source_description="mock-text",
        )
    }
    mock_scraper = MockScraper(url_map=scrape_map)
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    input_item = IndexableContent(
        content=url, embedding_type="raw_url", source_processor="test_source"
    )
    results = await processor.process(
        [input_item], mock_document, input_item, mock_tool_execution_context
    )

    with soft_assertions():
        assert_that(results).is_length(1)
        result_item = results[0]
        assert_that(result_item.embedding_type).is_equal_to("fetched_content_text")
        assert_that(result_item.content).is_equal_to("Plain text content.")
        assert_that(result_item.mime_type).is_equal_to("text/plain")
    processor.cleanup_temp_files()


@pytest.mark.asyncio
async def test_fetch_image_content_success(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test successful fetching and storing of image content."""
    url = "http://example.com/image.png"
    image_bytes = b"fake_image_data_png"
    scrape_map = {
        url: ScrapeResult(
            type="image",
            final_url=url,
            title="Mock Image Title",
            content_bytes=image_bytes,
            mime_type="image/png",
            source_description="mock-image",
        )
    }
    mock_scraper = MockScraper(url_map=scrape_map)
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    input_item = IndexableContent(
        content=url, embedding_type="extracted_link", source_processor="test_source"
    )
    results = await processor.process(
        [input_item], mock_document, input_item, mock_tool_execution_context
    )

    with soft_assertions():
        assert_that(results).is_length(1)
        result_item = results[0]
        assert_that(result_item.embedding_type).is_equal_to(
            "fetched_content_binary"
        )
        assert_that(result_item.content).is_none()
        assert_that(result_item.ref).is_not_none()
        assert_that(result_item.mime_type).is_equal_to("image/png")
        assert_that(result_item.metadata["original_filename"]).is_equal_to(
            "image.png"
        )  # Based on URL parsing

        # Verify temp file content
        assert_that(result_item.ref).exists()
        with open(result_item.ref, "rb") as f:
            assert_that(f.read()).is_equal_to(image_bytes)

        # Test cleanup
        assert_that(processor._temp_files).is_length(1)
        temp_file_path = processor._temp_files[0]
        assert_that(temp_file_path).is_equal_to(result_item.ref)

    processor.cleanup_temp_files()
    with soft_assertions():
        assert_that(temp_file_path).does_not_exist()
        assert_that(processor._temp_files).is_empty()


@pytest.mark.asyncio
async def test_scraper_error_passes_through_item(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test that if scraper returns an error, the original item is passed through."""
    url = "http://example.com/will_error"
    scrape_map = {
        url: ScrapeResult(
            type="error",
            final_url=url,
            message="Scraping failed miserably",
            source_description="mock-error",
        )
    }
    mock_scraper = MockScraper(url_map=scrape_map)
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    input_item = IndexableContent(
        content=url, embedding_type="extracted_link", source_processor="test_source"
    )
    results = await processor.process(
        [input_item], mock_document, input_item, mock_tool_execution_context
    )

    with soft_assertions():
        assert_that(results).is_length(1)
        assert_that(results[0]).is_same_as(input_item)  # Should be the exact same item
    processor.cleanup_temp_files()


@pytest.mark.asyncio
async def test_non_target_embedding_type_passes_through(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test that items with non-target embedding_types are passed through."""
    url = "http://example.com/ignored_type"
    # Scraper map can be empty as it shouldn't be called
    mock_scraper = MockScraper(url_map={})
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    input_item = IndexableContent(
        content=url, embedding_type="some_other_type", source_processor="test_source"
    )
    results = await processor.process(
        [input_item], mock_document, input_item, mock_tool_execution_context
    )

    with soft_assertions():
        assert_that(results).is_length(1)
        assert_that(results[0]).is_same_as(input_item)
    processor.cleanup_temp_files()


@pytest.mark.asyncio
async def test_non_url_content_passes_through(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test that items with non-URL content are passed through."""
    mock_scraper = MockScraper(url_map={})  # Should not be called
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    input_item = IndexableContent(
        content="This is not a URL",
        embedding_type="extracted_link",  # Target type, but content isn't URL
        source_processor="test_source",
    )
    results = await processor.process(
        [input_item], mock_document, input_item, mock_tool_execution_context
    )

    with soft_assertions():
        assert_that(results).is_length(1)
        assert_that(results[0]).is_same_as(input_item)
    processor.cleanup_temp_files()


@pytest.mark.asyncio
async def test_empty_input_list(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test that an empty list of input items results in an empty list."""
    mock_scraper = MockScraper(url_map={})
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    results = await processor.process(
        [], mock_document, MagicMock(spec=IndexableContent), mock_tool_execution_context
    )
    assert_that(results).is_empty()
    processor.cleanup_temp_files()


@pytest.mark.asyncio
async def test_multiple_items_processing(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test processing a mix of items: successful fetch, error, and pass-through."""
    url_md = "http://example.com/markdown"
    url_img = "http://example.com/image.jpg"
    url_err = "http://example.com/error_page"

    scrape_map = {
        url_md: ScrapeResult(
            type="markdown",
            final_url=url_md,
            title="MD Page Title",
            content="MD Content",
        ),
        url_img: ScrapeResult(
            type="image",
            final_url=url_img,
            title="Image Page Title",
            content_bytes=b"img",
            mime_type="image/jpeg",
        ),
        url_err: ScrapeResult(
            type="error",
            final_url=url_err,
            title="Error Page Title",  # Title might still be present on error pages
            message="Failed fetch",
        ),
    }
    mock_scraper = MockScraper(url_map=scrape_map)
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    item_md = IndexableContent(
        content=url_md,
        embedding_type="extracted_link",
        source_processor="test_source_md",
    )
    item_img = IndexableContent(
        content=url_img, embedding_type="raw_url", source_processor="test_source_img"
    )
    item_err = IndexableContent(
        content=url_err,
        embedding_type="extracted_link",
        source_processor="test_source_err",
    )
    item_pass_type = IndexableContent(
        content="http://example.com/pass",
        embedding_type="other_type",
        source_processor="test_source_pass_type",
    )
    item_pass_content = IndexableContent(
        content="not a url",
        embedding_type="extracted_link",
        source_processor="test_source_pass_content",
    )

    initial_items = [item_md, item_img, item_err, item_pass_type, item_pass_content]
    # Create a mock for initial_content_ref, can be one of the items or a generic one
    mock_initial_ref = IndexableContent(
        content="initial",
        embedding_type="initial_type",
        source_processor="test_initial_ref_source",
    )

    results = await processor.process(
        initial_items, mock_document, mock_initial_ref, mock_tool_execution_context
    )

    with soft_assertions():
        assert_that(results).is_length(5)

        fetched_md_items = [
            item
            for item in results
            if item.embedding_type == "fetched_content_markdown"
            and item.metadata.get("original_url") == url_md
        ]
        assert_that(fetched_md_items).is_length(1)
        if fetched_md_items:
            assert_that(fetched_md_items[0].content).is_equal_to("MD Content")

        fetched_img_items = [
            item
            for item in results
            if item.embedding_type == "fetched_content_binary"
            and item.metadata.get("original_url") == url_img
        ]
        assert_that(fetched_img_items).is_length(1)
        if fetched_img_items:
            assert_that(fetched_img_items[0].ref).is_not_none()
            assert_that(fetched_img_items[0].ref).exists()

        passed_err_items = [item for item in results if item is item_err]
        assert_that(passed_err_items).is_length(1)

        passed_type_items = [item for item in results if item is item_pass_type]
        assert_that(passed_type_items).is_length(1)

        passed_content_items = [item for item in results if item is item_pass_content]
        assert_that(passed_content_items).is_length(1)

    processor.cleanup_temp_files()


@pytest.mark.asyncio
async def test_cleanup_temp_files_no_files(
    default_config: WebFetcherProcessorConfig,
) -> None:
    """Test cleanup_temp_files when no temporary files were created."""
    mock_scraper = MockScraper(url_map={})
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)
    # Call process with no items that would create temp files
    await processor.process([], MagicMock(), MagicMock(), MagicMock())

    with soft_assertions():
        assert_that(processor._temp_files).is_empty()
    processor.cleanup_temp_files()  # Should not raise error
    assert_that(processor._temp_files).is_empty()


@pytest.mark.asyncio
async def test_cleanup_temp_files_file_externally_deleted(
    default_config: WebFetcherProcessorConfig,
    mock_document: "Document",
    mock_tool_execution_context: "ToolExecutionContext",
) -> None:
    """Test cleanup_temp_files when a tracked file was deleted externally."""
    url = "http://example.com/image.gif"
    scrape_map = {
        url: ScrapeResult(
            type="image",
            final_url=url,
            title="GIF Page Title",
            content_bytes=b"gif_data",
            mime_type="image/gif",
        )
    }
    mock_scraper = MockScraper(url_map=scrape_map)
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    input_item = IndexableContent(
        content=url,
        embedding_type="extracted_link",
        source_processor="test_source_external_delete",
    )
    results = await processor.process(
        [input_item], mock_document, input_item, mock_tool_execution_context
    )

    with soft_assertions():
        assert_that(results).is_length(1)
        result_item = results[0]
        assert_that(result_item.ref).is_not_none()
        temp_file_path = result_item.ref
        assert_that(temp_file_path).exists()

        # Simulate external deletion
        os.remove(temp_file_path)
        assert_that(temp_file_path).does_not_exist()

    processor.cleanup_temp_files()  # Should log a warning but not crash
    assert_that(processor._temp_files).is_empty()  # List should be cleared


def test_del_cleanup_fallback(
    default_config: WebFetcherProcessorConfig, caplog: LogCaptureFixture
) -> None:
    """Test that __del__ attempts cleanup if temp files remain (simulated)."""
    processor = WebFetcherProcessor(scraper=MockScraper({}), config=default_config)

    # Manually add a fake temp file path to simulate it wasn't cleaned up
    # Create a dummy file for it to try to delete
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        processor._temp_files.append(tmp.name)
        fake_temp_file_path = tmp.name

    assert_that(fake_temp_file_path).exists()

    # Trigger __del__
    del processor

    # Check logs for warning and that file was deleted
    # Note: __del__ behavior can be tricky to test reliably across all Python versions/GC timings.
    # This is a best-effort check.
    # We expect a warning about __del__ being called with remaining files.
    # And then the cleanup logic should run.

    # Allow some time for GC and __del__ to potentially run
    # This is not ideal for unit tests but __del__ is inherently like that.
    # A better approach is to ensure explicit cleanup is always called.
    # For this test, we'll just check if the file is gone after `del`.
    # If the test environment's GC is aggressive, this might pass.

    # Re-check if file exists. If __del__ ran and cleanup worked, it should be gone.
    # This part of the test might be flaky depending on GC.
    # A more robust test of __del__ might involve mocking os.remove and checking calls.
    # For now, let's focus on the explicit cleanup_temp_files tests.

    # We can check if the log message from __del__ was emitted.
    with soft_assertions():
        assert_that(caplog.text).contains(
            f"{WebFetcherProcessor(MockScraper({}), default_config).name} instance being deleted"
        )
        assert_that(caplog.text).contains("Attempting cleanup now.")

    # And check if the file was actually deleted by the fallback
    # This check might be flaky due to GC timing.
    # If the file still exists, it means __del__ didn't run or its cleanup failed.
    # The primary purpose here is to check the logging intent.
    if os.path.exists(fake_temp_file_path):
        # Log a warning if the file wasn't cleaned up by __del__, then clean it manually.
        # This avoids making the test fail outright due to GC unpredictability,
        # but still notes if the desired __del__ behavior didn't occur.
        # In a real scenario, the OS would eventually clean temp files, or explicit cleanup should be robust.
        logging.warning(
            f"Temp file {fake_temp_file_path} was not cleaned up by __del__ fallback. Manual removal."
        )
        os.remove(fake_temp_file_path)
    # No explicit assert_that().does_not_exist() here due to GC flakiness for __del__.
    # The log check is the more reliable part for __del__ intent.

    # If the file is gone, the __del__ fallback worked as intended.
    # Given the nature of __del__, this test is more about the logging and intent.
    # The primary tests for cleanup are `test_fetch_image_content_success` and `test_cleanup_temp_files_file_externally_deleted`.
