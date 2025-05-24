import json
import logging
import pathlib
import tempfile
from collections.abc import Generator
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from family_assistant.indexing.pipeline import IndexableContent
from family_assistant.indexing.processors.llm_processors import (
    LLMIntelligenceProcessor,
)

# Assuming RuleBasedMockLLMClient is correctly exposed or imported
# Import LLMOutput from the same module as RuleBasedMockLLMClient to ensure type consistency for rules
if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface as RealLLMInterface
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


@pytest.fixture
def temp_text_file() -> Generator[pathlib.Path, None, None]:
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

    # 1. Define a matcher for the mock LLM's generate_response method
    def generate_response_matcher(kwargs: MatcherArgs) -> bool:
        # The matcher now only receives kwargs for generate_response.
        # The _method_name_for_matcher check is no longer needed.
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:  # Expect system + user
            return False

        system_message = messages[0]
        user_message = messages[1]

        # Check system prompt
        if (
            system_message.get("role") != "system"
            or system_message.get("content")
            != "Extract a concise summary from the provided document."
        ):
            return False

        # Check user message structure (assuming mock_format_user_message_with_file created this structure)
        if user_message.get("role") != "user":
            return False

        content = user_message.get("content")
        if not isinstance(content, list) or len(content) < 2:
            return False  # Expecting text part and file part

        text_part = content[0]
        file_part = content[1]

        if not (
            text_part.get("type") == "text"
            and text_part.get("text") == "Please summarize the attached file."
        ):
            return False

        # Check the mock file placeholder part
        if not (
            file_part.get("type") == "file_placeholder"
            and file_part.get("file_reference", {}).get("file_path")
            == str(temp_text_file)
            and file_part.get("file_reference", {}).get("mime_type") == "text/plain"
        ):
            return False

        # Check tool choice
        if kwargs.get("tool_choice")["function"]["name"] != tool_name_for_extraction:  # type: ignore[index]
            return False

        logger.debug(f"generate_response_matcher matched with kwargs: {kwargs}")
        return True

    # 2. Define the LLMOutput the mock should return for generate_response
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
    # No specific rules needed for format_user_message_with_file as its mock is simple.
    # The rule applies to generate_response.
    mock_llm_client = RuleBasedMockLLMClient(
        rules=[(generate_response_matcher, mock_llm_tool_call_output)],
        model_name="mock-summarizer-v1",
    )

    # 4. Instantiate LLMIntelligenceProcessor
    processor_output_schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "A concise summary"}
        },
        "required": ["summary"],
        "description": "Schema for extracted summary.",
    }
    llm_processor = LLMIntelligenceProcessor(
        llm_client=cast("RealLLMInterface", mock_llm_client),
        system_prompt_template="Extract a concise summary from the provided document.",
        output_schema=processor_output_schema,
        target_embedding_type="document_summary_from_file",
        input_content_types=["original_document_file"],
        tool_name=tool_name_for_extraction,
        max_content_length=None,  # No truncation for this test
    )

    # 5. Create an IndexableContent item pointing to the temp file
    input_item = IndexableContent(
        embedding_type="original_document_file",
        source_processor="test_setup",
        ref=str(temp_text_file),
        mime_type="text/plain",
        content="Please summarize the attached file.",  # This is the prompt_text for the LLM
    )

    # 6. Mock ToolExecutionContext and other arguments for processor.process
    mock_exec_context = MagicMock()
    mock_original_doc = MagicMock()
    mock_original_doc.title = "Test Document Title"  # For logging in pipeline

    # 7. Call processor.process
    processed_items = await llm_processor.process(
        current_items=[input_item],
        original_document=mock_original_doc,
        initial_content_ref=input_item,  # For this test, it's the same
        context=mock_exec_context,
    )

    # 8. Assertions
    assert len(processed_items) == 2, (
        f"Expected two items (original + new), got {len(processed_items)}"
    )

    # Check the original item is passed through (should be the first one)
    original_item_passed = processed_items[0]
    assert original_item_passed is input_item, "Original item was not passed through"

    # Check the newly created summary item (should be the second one)
    new_item = processed_items[1]
    assert new_item.embedding_type == "document_summary_from_file", (
        f"Expected embedding_type 'document_summary_from_file', got '{new_item.embedding_type}'"
    )
    assert new_item.source_processor == llm_processor.name, (
        f"Expected source_processor '{llm_processor.name}', got '{new_item.source_processor}'"
    )
    assert new_item.mime_type == "application/json", (
        f"Expected mime_type 'application/json', got '{new_item.mime_type}'"
    )
    assert new_item.content is not None, "Newly created item content should not be None"
    extracted_content_data = json.loads(new_item.content)
    assert extracted_content_data == expected_extracted_data, (
        f"Content mismatch. Expected:\n{json.dumps(expected_extracted_data, indent=2)}\nGot:\n{new_item.content}"
    )
    assert (
        new_item.metadata.get("original_item_embedding_type")
        == "original_document_file"
    ), "Metadata 'original_item_embedding_type' mismatch"
    assert new_item.metadata.get("original_item_source_processor") == "test_setup", (
        "Metadata 'original_item_source_processor' mismatch"
    )
    assert new_item.metadata.get("llm_model_used") == "mock-summarizer-v1", (
        "Metadata 'llm_model_used' mismatch"
    )

    # Assert mock LLM was called correctly
    calls = mock_llm_client.get_calls()
    assert len(calls) == 2  # format_user_message_with_file + generate_response

    format_call = next(
        c for c in calls if c["method_name"] == "format_user_message_with_file"
    )
    generate_call = next(c for c in calls if c["method_name"] == "generate_response")

    # Assert call to format_user_message_with_file
    format_call_args = format_call["kwargs"]
    assert format_call_args.get("file_path") == str(temp_text_file)
    assert format_call_args.get("mime_type") == "text/plain"
    assert format_call_args.get("prompt_text") == "Please summarize the attached file."
    assert format_call_args.get("max_text_length") is None  # As set in processor

    # Assert call to generate_response (already partially checked by matcher)
    generate_call_args = generate_call["kwargs"]
    assert generate_call_args.get("messages")[0]["role"] == "system"  # type: ignore[index]
    assert (
        generate_call_args.get("messages")[0]["content"]
        == "Extract a concise summary from the provided document."
    )  # type: ignore[index]
    # User message structure is verified by the matcher.
    assert (
        generate_call_args.get("tools")[0]["function"]["name"]
        == tool_name_for_extraction
    )  # type: ignore[index]
    assert (
        generate_call_args.get("tools")[0]["function"]["parameters"]
        == processor_output_schema
    )  # type: ignore[index]
    assert (
        generate_call_args.get("tool_choice")["function"]["name"]
        == tool_name_for_extraction
    )  # type: ignore[index]
