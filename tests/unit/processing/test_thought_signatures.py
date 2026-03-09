from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing.utils import (
    assistant_message_has_thought_signature,
    messages_have_thought_signatures,
)
from tests.factories.messages import (
    create_assistant_message,
    create_tool_call,
    create_user_message,
)


def test_assistant_message_has_thought_signature_false_without_tool_calls() -> None:
    message = create_assistant_message(content="No tool call", tool_calls=None)

    assert not assistant_message_has_thought_signature(message)


def test_signature_detected_for_google_metadata() -> None:
    message = create_assistant_message(
        content=None,
        tool_calls=[
            ToolCallItem(
                id="call_with_signature",
                type="function",
                function=ToolCallFunction(
                    name="test_tool",
                    arguments="{}",
                ),
                provider_metadata={
                    "provider": "google",
                    "thought_signature": "signed",
                },
            )
        ],
    )

    assert assistant_message_has_thought_signature(message)


def test_signature_not_detected_for_non_google_metadata() -> None:
    message = create_assistant_message(
        content=None,
        tool_calls=[
            ToolCallItem(
                id="call_without_signature",
                type="function",
                function=ToolCallFunction(
                    name="test_tool",
                    arguments="{}",
                ),
                provider_metadata={
                    "provider": "openai",
                    "thought_signature": "signed",
                },
            )
        ],
    )

    assert not assistant_message_has_thought_signature(message)


def test_messages_have_thought_signatures_detects_signature_in_any_message() -> None:
    signed_assistant = create_assistant_message(
        content=None,
        tool_calls=[
            ToolCallItem(
                id="call_with_signature",
                type="function",
                function=ToolCallFunction(
                    name="test_tool",
                    arguments="{}",
                ),
                provider_metadata={
                    "provider": "google",
                    "thought_signature": "signed",
                },
            )
        ],
    )
    unsigned_assistant = create_assistant_message(
        content="Plain tool call",
        tool_calls=[create_tool_call()],
    )

    assert messages_have_thought_signatures([
        create_user_message("hello"),
        unsigned_assistant,
        signed_assistant,
    ])


def test_messages_have_thought_signatures_false_without_google_signature() -> None:
    unsigned_assistant = create_assistant_message(
        content="Plain tool call",
        tool_calls=[create_tool_call()],
    )

    assert not messages_have_thought_signatures([
        create_user_message("hello"),
        unsigned_assistant,
    ])
