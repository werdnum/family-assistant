"""
Simple one-shot LLM call utilities for scripts.

This module provides easy-to-use functions for making one-shot LLM calls
without needing to set up the full LLM infrastructure. Ideal for scripts
that need summarization, data extraction, or simple text generation.

Usage:
    from family_assistant.llm.one_shot import one_shot, one_shot_structured

    # Simple text generation
    response = await one_shot("Summarize this text: ...")

    # With system prompt
    response = await one_shot(
        "Extract the key points",
        system="You are a helpful assistant that extracts key information."
    )

    # Structured output
    from pydantic import BaseModel

    class Summary(BaseModel):
        title: str
        key_points: list[str]

    result = await one_shot_structured(
        "Summarize this article: ...",
        response_model=Summary
    )
"""

from pydantic import BaseModel

from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import SystemMessage, UserMessage

DEFAULT_MODEL = "gemini-2.5-flash-preview-05-20"


async def one_shot(
    prompt: str,
    *,
    system: str | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Make a simple one-shot LLM call.

    Args:
        prompt: The user prompt to send to the LLM.
        system: Optional system message to set context.
        model: Model to use (default: gemini-2.5-flash-preview-05-20).

    Returns:
        The LLM's text response.

    Raises:
        ValueError: If the LLM returns no content.

    Example:
        response = await one_shot("Summarize: The quick brown fox...")
        print(response)
    """
    client = LLMClientFactory.create_client({"model": model})

    messages: list[SystemMessage | UserMessage] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(UserMessage(content=prompt))

    result = await client.generate_response(messages)

    if result.content is None:
        raise ValueError("LLM returned no content")

    return result.content


async def one_shot_structured[T: BaseModel](
    prompt: str,
    response_model: type[T],
    *,
    system: str | None = None,
    model: str = DEFAULT_MODEL,
) -> T:
    """
    Make a one-shot LLM call with structured output.

    Args:
        prompt: The user prompt to send to the LLM.
        response_model: Pydantic model class for the expected response structure.
        system: Optional system message to set context.
        model: Model to use (default: gemini-2.5-flash-preview-05-20).

    Returns:
        An instance of response_model populated with the LLM's response.

    Example:
        from pydantic import BaseModel

        class Summary(BaseModel):
            title: str
            key_points: list[str]

        result = await one_shot_structured(
            "Summarize this article about AI...",
            response_model=Summary
        )
        print(result.title)
        print(result.key_points)
    """
    client = LLMClientFactory.create_client({"model": model})

    messages: list[SystemMessage | UserMessage] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(UserMessage(content=prompt))

    return await client.generate_structured(messages, response_model)
