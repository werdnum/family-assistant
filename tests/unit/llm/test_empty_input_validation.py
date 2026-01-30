"""Unit tests for empty input validation in LLM clients."""

import pytest

from family_assistant.llm import BaseLLMClient
from family_assistant.llm.base import InvalidRequestError
from family_assistant.llm.messages import (
    ImageUrlContentPart,
    SystemMessage,
    TextContentPart,
    UserMessage,
)


class MockBaseLLMClient(BaseLLMClient):
    """Mock implementation of BaseLLMClient for testing validation."""

    def __init__(self, model: str = "test-model") -> None:
        self.model = model


@pytest.mark.no_db
class TestEmptyInputValidation:
    """Tests for _validate_user_input method."""

    def test_empty_string_raises_error(self) -> None:
        """Empty string content should raise InvalidRequestError."""
        client = MockBaseLLMClient()
        messages = [UserMessage(content="")]

        with pytest.raises(InvalidRequestError) as exc_info:
            client._validate_user_input(messages)

        assert "User message cannot be empty" in str(exc_info.value)

    def test_whitespace_only_raises_error(self) -> None:
        """Whitespace-only content should raise InvalidRequestError."""
        client = MockBaseLLMClient()
        messages = [UserMessage(content="   \n\t  ")]

        with pytest.raises(InvalidRequestError) as exc_info:
            client._validate_user_input(messages)

        assert "User message cannot be empty" in str(exc_info.value)

    def test_empty_list_raises_error(self) -> None:
        """Empty content list should raise InvalidRequestError."""
        client = MockBaseLLMClient()
        messages = [UserMessage(content=[])]

        with pytest.raises(InvalidRequestError) as exc_info:
            client._validate_user_input(messages)

        assert "User message cannot be empty" in str(exc_info.value)

    def test_list_with_empty_text_parts_raises_error(self) -> None:
        """List containing only empty text parts should raise InvalidRequestError."""
        client = MockBaseLLMClient()
        messages = [
            UserMessage(
                content=[
                    TextContentPart(type="text", text=""),
                    TextContentPart(type="text", text="   "),
                ]
            )
        ]

        with pytest.raises(InvalidRequestError) as exc_info:
            client._validate_user_input(messages)

        assert "User message cannot be empty" in str(exc_info.value)

    def test_valid_string_content_passes(self) -> None:
        """Valid string content should not raise an error."""
        client = MockBaseLLMClient()
        messages = [UserMessage(content="Hello, world!")]

        # Should not raise
        client._validate_user_input(messages)

    def test_valid_text_part_passes(self) -> None:
        """Valid text content part should not raise an error."""
        client = MockBaseLLMClient()
        messages = [
            UserMessage(content=[TextContentPart(type="text", text="Hello, world!")])
        ]

        # Should not raise
        client._validate_user_input(messages)

    def test_image_content_part_passes(self) -> None:
        """Image content part should count as valid content."""
        client = MockBaseLLMClient()
        messages = [
            UserMessage(
                content=[
                    ImageUrlContentPart(
                        type="image_url",
                        image_url={"url": "data:image/png;base64,abc123"},
                    )
                ]
            )
        ]

        # Should not raise - images count as valid content
        client._validate_user_input(messages)

    def test_mixed_empty_text_and_image_passes(self) -> None:
        """Mixed content with empty text but valid image should pass."""
        client = MockBaseLLMClient()
        messages = [
            UserMessage(
                content=[
                    TextContentPart(type="text", text=""),
                    ImageUrlContentPart(
                        type="image_url",
                        image_url={"url": "data:image/png;base64,abc123"},
                    ),
                ]
            )
        ]

        # Should not raise - has valid image content
        client._validate_user_input(messages)

    def test_no_user_message_passes(self) -> None:
        """Messages without UserMessage should pass validation."""
        client = MockBaseLLMClient()
        messages = [SystemMessage(content="You are a helpful assistant.")]

        # Should not raise - no user message to validate
        client._validate_user_input(messages)

    def test_validates_last_user_message(self) -> None:
        """Should validate only the last user message."""
        client = MockBaseLLMClient()
        messages = [
            UserMessage(content="First message"),
            SystemMessage(content="System prompt"),
            UserMessage(content=""),  # This is the last user message, empty
        ]

        with pytest.raises(InvalidRequestError) as exc_info:
            client._validate_user_input(messages)

        assert "User message cannot be empty" in str(exc_info.value)

    def test_valid_message_after_empty_passes(self) -> None:
        """Valid last user message should pass even if earlier ones were empty."""
        client = MockBaseLLMClient()
        messages = [
            UserMessage(content=""),  # First is empty, but we check last
            UserMessage(content="Valid message"),  # Last is valid
        ]

        # Should not raise - last user message is valid
        client._validate_user_input(messages)

    def test_error_includes_model_info(self) -> None:
        """InvalidRequestError should include model information."""
        client = MockBaseLLMClient(model="openai/gpt-4")
        messages = [UserMessage(content="")]

        with pytest.raises(InvalidRequestError) as exc_info:
            client._validate_user_input(messages)

        error = exc_info.value
        assert error.provider == "openai"
        assert error.model == "openai/gpt-4"
