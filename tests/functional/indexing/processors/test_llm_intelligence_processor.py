import json
import logging
import pathlib
import tempfile
from unittest.mock import MagicMock

import pytest

from family_assistant.indexing.pipeline import IndexableContent
from family_assistant.indexing.processors.llm_processors import (
    LLMIntelligenceProcessor,
)
from family_assistant.llm import LLMOutput

# Assuming RuleBasedMockLLMClient is correctly exposed or imported
from tests.mocks.mock_llm import MatcherArgs, Rule, RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


@pytest.fixture
def temp_text_file() -> pathlib.Path:
    """Creates a temporary text file with some content."""
    content = "This is a test document for summarization by the LLM."
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix=".txt", encoding="utf-8"
    ) as tmp_file:
        tmp_file.write(content)
        path = pathlib.Path(tmp_file.name)
    yield path
    path.unlink()  # Clean up the file


@pytest.mark.asyncio
async def test_llm_processor_with_file_input_summarization(
    temp_text_file: pathlib.Path,
) -> None:
    """
    Tests LLMIntelligenceProcessor with a file input, expecting it to call the LLM
    and produce a summary based on the mock LLM's tool call response.
    """
    expected_summary = "This is the LLM's summary of the test document."
    expected_extracted_data = {"summary": expected_summary}
    tool_name_for_extraction = "extract_summary_tool"

    # 1. Define a matcher for the mock LLM
    def summary_matcher(method_name: str, kwargs: MatcherArgs) -> bool:
        if method_name != "generate_response_from_file_input":
            return False
        logger.debug(f"Matcher received kwargs: {kwargs}")
        # Check if the file path matches and other relevant args
        return (
            kwargs.get("file_path") == str(temp_text_file)
            and kwargs.get("mime_type") == "text/plain"
            and kwargs.get("system_prompt")
            == "Extract a concise summary from the provided document."
            and kwargs.get("tool_choice")["function"]["name"]  # type: ignore[index]
            == tool_name_for_extraction
        )

    # 2. Define the LLMOutput the mock should return
    mock_llm_tool_call_output = LLMOutput(
        tool_calls=[
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": tool_name_for_extraction,
                    "arguments": json.dumps(expected_extracted_data),
                },
            }
        ]
    )

    # 3. Instantiate RuleBasedMockLLMClient
    mock_llm_client = RuleBasedMockLLMClient(
        rules=[(summary_matcher, mock_llm_tool_call_output)],
        model_name="mock-summarizer-v1",
    )

    # 4. Instantiate LLMIntelligenceProcessor
    processor_output_schema = {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "A concise summary"}},
        "required": ["summary"],
        "description": "Schema for extracted summary.",
    }
    llm_processor = LLMIntelligenceProcessor(
        llm_client=mock_llm_client,
        system_prompt_template="Extract a concise summary from the provided document.",
        output_schema=processor_output_schema,
        target_embedding_type="document_summary_from_file",
        input_content_types=["original_document_file"],
        tool_name=tool_name_for_extraction,
        max_content_length=None, # No truncation for this test
    )

    # 5. Create an IndexableContent item pointing to the temp file
    input_item = IndexableContent(
        embedding_type="original_document_file",
        source_processor="test_setup",
        ref=str(temp_text_file),
        mime_type="text/plain",
        content="Please summarize the attached file.", # This is the prompt_text for the LLM
    )

    # 6. Mock ToolExecutionContext and other arguments for processor.process
    mock_exec_context = MagicMock()
    mock_original_doc = MagicMock()
    mock_original_doc.title = "Test Document Title" # For logging in pipeline

    # 7. Call processor.process
    processed_items = await llm_processor.process(
        current_items=[input_item],
        original_document=mock_original_doc,
        initial_content_ref=input_item, # For this test, it's the same
        context=mock_exec_context,
    )

    # 8. Assertions
    assert len(processed_items) == 1, "Expected one new item to be created"
    new_item = processed_items[0]

    assert new_item.embedding_type == "document_summary_from_file"
    assert new_item.mime_type == "application/json"
    assert new_item.content is not None
    extracted_content_data = json.loads(new_item.content)
    assert extracted_content_data == expected_extracted_data
    assert new_item.source_processor == llm_processor.name
    assert new_item.metadata.get("original_item_embedding_type") == "original_document_file"
    assert new_item.metadata.get("llm_model_used") == "mock-summarizer-v1"

    # Assert mock LLM was called correctly
    calls = mock_llm_client.get_calls()
    assert len(calls) == 1
    call_args = calls[0]["kwargs"]
    assert calls[0]["method_name"] == "generate_response_from_file_input"
    assert call_args.get("file_path") == str(temp_text_file)
    assert call_args.get("mime_type") == "text/plain"
    assert call_args.get("prompt_text") == "Please summarize the attached file."
    assert call_args.get("system_prompt") == "Extract a concise summary from the provided document."
    assert call_args.get("tools")[0]["function"]["name"] == tool_name_for_extraction # type: ignore[index]
    assert call_args.get("tools")[0]["function"]["parameters"] == processor_output_schema # type: ignore[index]
    assert call_args.get("tool_choice")["function"]["name"] == tool_name_for_extraction # type: ignore[index]
