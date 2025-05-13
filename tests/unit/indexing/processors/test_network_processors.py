"""Unit tests for network_processors.py."""

import os
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from _pytest.logging import LogCaptureFixture

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

    assert len(results) == 1
    result_item = results[0]
    assert result_item.embedding_type == "fetched_content_markdown"
    assert result_item.content == "## Hello Markdown"
    assert result_item.mime_type == "text/markdown"
    assert result_item.source_processor == processor.name
    assert result_item.metadata["original_url"] == url
    assert result_item.metadata["source_scraper_description"] == "mock-markdown"
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

    assert len(results) == 1
    result_item = results[0]
    assert result_item.embedding_type == "fetched_content_text"
    assert result_item.content == "Plain text content."
    assert result_item.mime_type == "text/plain"
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

    assert len(results) == 1
    result_item = results[0]
    assert result_item.embedding_type == "fetched_content_binary"
    assert result_item.content is None
    assert result_item.ref is not None
    assert result_item.mime_type == "image/png"
    assert (
        result_item.metadata["original_filename"] == "image.png"
    )  # Based on URL parsing

    # Verify temp file content
    assert os.path.exists(result_item.ref)
    with open(result_item.ref, "rb") as f:
        assert f.read() == image_bytes

    # Test cleanup
    assert len(processor._temp_files) == 1
    temp_file_path = processor._temp_files[0]
    assert temp_file_path == result_item.ref

    processor.cleanup_temp_files()
    assert not os.path.exists(temp_file_path)
    assert len(processor._temp_files) == 0


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

    assert len(results) == 1
    assert results[0] is input_item  # Should be the exact same item
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

    assert len(results) == 1
    assert results[0] is input_item
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

    assert len(results) == 1
    assert results[0] is input_item
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
    assert len(results) == 0
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
        url_md: ScrapeResult(type="markdown", final_url=url_md, content="MD Content"),
        url_img: ScrapeResult(
            type="image",
            final_url=url_img,
            content_bytes=b"img",
            mime_type="image/jpeg",
        ),
        url_err: ScrapeResult(type="error", final_url=url_err, message="Failed fetch"),
    }
    mock_scraper = MockScraper(url_map=scrape_map)
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    item_md = IndexableContent(content=url_md, embedding_type="extracted_link", source_processor="test_source_md")
    item_img = IndexableContent(content=url_img, embedding_type="raw_url", source_processor="test_source_img")
    item_err = IndexableContent(content=url_err, embedding_type="extracted_link", source_processor="test_source_err")
    item_pass_type = IndexableContent(
        content="http://example.com/pass", embedding_type="other_type", source_processor="test_source_pass_type"
    )
    item_pass_content = IndexableContent(
        content="not a url", embedding_type="extracted_link", source_processor="test_source_pass_content"
    )

    initial_items = [item_md, item_img, item_err, item_pass_type, item_pass_content]
    # Create a mock for initial_content_ref, can be one of the items or a generic one
    mock_initial_ref = IndexableContent(
        content="initial", embedding_type="initial_type", source_processor="test_initial_ref_source"
    )

    results = await processor.process(
        initial_items, mock_document, mock_initial_ref, mock_tool_execution_context
    )

    assert len(results) == 5

    fetched_md_count = 0
    fetched_img_count = 0
    passed_err_count = 0
    passed_type_count = 0
    passed_content_count = 0

    for r_item in results:
        if (
            r_item.embedding_type == "fetched_content_markdown"
            and r_item.metadata.get("original_url") == url_md
        ):
            fetched_md_count += 1
            assert r_item.content == "MD Content"
        elif (
            r_item.embedding_type == "fetched_content_binary"
            and r_item.metadata.get("original_url") == url_img
        ):
            fetched_img_count += 1
            assert r_item.ref is not None
            assert os.path.exists(r_item.ref)
        elif r_item is item_err:  # Error items are passed through
            passed_err_count += 1
        elif r_item is item_pass_type:
            passed_type_count += 1
        elif r_item is item_pass_content:
            passed_content_count += 1

    assert fetched_md_count == 1
    assert fetched_img_count == 1
    assert passed_err_count == 1
    assert passed_type_count == 1
    assert passed_content_count == 1

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

    assert len(processor._temp_files) == 0
    processor.cleanup_temp_files()  # Should not raise error
    assert len(processor._temp_files) == 0


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
            content_bytes=b"gif_data",
            mime_type="image/gif",
        )
    }
    mock_scraper = MockScraper(url_map=scrape_map)
    processor = WebFetcherProcessor(scraper=mock_scraper, config=default_config)

    input_item = IndexableContent(content=url, embedding_type="extracted_link")
    results = await processor.process(
        [input_item], mock_document, input_item, mock_tool_execution_context
    )

    assert len(results) == 1
    result_item = results[0]
    assert result_item.ref is not None
    temp_file_path = result_item.ref
    assert os.path.exists(temp_file_path)

    # Simulate external deletion
    os.remove(temp_file_path)
    assert not os.path.exists(temp_file_path)

    processor.cleanup_temp_files()  # Should log a warning but not crash
    assert len(processor._temp_files) == 0  # List should be cleared


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

    assert os.path.exists(fake_temp_file_path)

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
    assert (
        f"{WebFetcherProcessor(MockScraper({}), default_config).name} instance being deleted"
        in caplog.text
    )
    assert "Attempting cleanup now." in caplog.text

    # And check if the file was actually deleted by the fallback
    if os.path.exists(fake_temp_file_path):
        os.remove(fake_temp_file_path)  # Clean up if __del__ didn't get it
        pytest.fail(
            f"__del__ fallback cleanup did not remove the temp file: {fake_temp_file_path}"
        )

    # If the file is gone, the __del__ fallback worked as intended.
    # If the test fails here, it means __del__ didn't run or its cleanup failed.
    # Given the nature of __del__, this test is more about the logging and intent.
    # The primary tests for cleanup are `test_fetch_image_content_success` and `test_cleanup_temp_files_file_externally_deleted`.
