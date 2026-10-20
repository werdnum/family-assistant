"""One-shot LLM call utilities.

Convenience functions for making single LLM calls without managing
client lifecycle or message construction.
"""

from collections.abc import Mapping

from pydantic import BaseModel

from family_assistant.llm import JsonObject, LLMInterface
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import LLMMessage, SystemMessage, UserMessage

DEFAULT_MODEL = "gemini-3.8-flash"

# The deployment's model-keyed `llm_parameters` map, as AppConfig holds it.
LLMParameters = Mapping[str, Mapping[str, object]]


def _client(model: str, llm_parameters: LLMParameters | None) -> LLMInterface:
    """A client for ``model``, served with the deployment's request parameters."""
    if llm_parameters is None:
        return LLMClientFactory.create_client({"model": model})
    return LLMClientFactory.create_client({
        "model": model,
        "model_parameters": llm_parameters,
    })


async def one_shot(
    prompt: str,
    *,
    system: str | None = None,
    model: str = DEFAULT_MODEL,
    llm_parameters: LLMParameters | None = None,
) -> str:
    """Make a single LLM call and return the text response.

    Args:
        prompt: The user prompt to send.
        system: Optional system message.
        model: Model identifier (default: gemini-3.8-flash).
        llm_parameters: The deployment's ``llm_parameters`` map, so the call
            gets the same request parameters the model is configured with.

    Returns:
        The text content of the LLM response.

    Raises:
        ValueError: If the LLM returns no content.
    """
    client = _client(model, llm_parameters)

    messages: list[LLMMessage] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(UserMessage(content=prompt))

    result = await client.generate_response(messages)

    if not result.content:
        raise ValueError("LLM returned no content")

    return result.content


async def one_shot_structured[T: BaseModel](
    prompt: str,
    *,
    response_model: type[T],
    system: str | None = None,
    model: str = DEFAULT_MODEL,
    llm_parameters: LLMParameters | None = None,
) -> T:
    """Make a single LLM call and return a structured response.

    Args:
        prompt: The user prompt to send.
        response_model: Pydantic model class for the response.
        system: Optional system message.
        model: Model identifier (default: gemini-3.8-flash).
        llm_parameters: The deployment's ``llm_parameters`` map, so the call
            gets the same request parameters the model is configured with.

    Returns:
        An instance of response_model populated from the LLM response.
    """
    client = _client(model, llm_parameters)

    messages: list[LLMMessage] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(UserMessage(content=prompt))

    return await client.generate_structured(messages, response_model)


async def one_shot_json(
    prompt: str,
    *,
    system: str | None = None,
    model: str = DEFAULT_MODEL,
    llm_parameters: LLMParameters | None = None,
) -> JsonObject:
    """Make a single LLM call and return a parsed JSON object.

    Uses native JSON mode when available (OpenAI, Gemini) for more
    reliable JSON output.

    Args:
        prompt: The user prompt to send.
        system: Optional system message.
        model: Model identifier (default: gemini-3.8-flash).
        llm_parameters: The deployment's ``llm_parameters`` map, so the call
            gets the same request parameters the model is configured with.

    Returns:
        Parsed JSON object (dict).

    Raises:
        StructuredOutputError: If response cannot be parsed as JSON.
    """
    client = _client(model, llm_parameters)

    messages: list[LLMMessage] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(UserMessage(content=prompt))

    return await client.generate_json(messages)
