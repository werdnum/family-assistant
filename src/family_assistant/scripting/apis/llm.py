"""LLM API for the scripting engine.

Provides llm() and llm_json() functions for scripts to make one-shot
LLM calls for summarisation, data extraction, and similar tasks.
"""

import json
import logging
import re
from typing import Any

from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import SystemMessage, UserMessage

DEFAULT_MODEL = "gemini-3-flash-preview"

logger = logging.getLogger(__name__)


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences from LLM JSON responses."""
    stripped = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    stripped = re.sub(r"\n?```\s*$", "", stripped)
    return stripped.strip()


async def llm_call_async(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
) -> str:
    """Make a one-shot LLM call and return the text response.

    Args:
        prompt: The user prompt to send.
        system: Optional system message.
        model: Model identifier (default: gemini-3-flash-preview).

    Returns:
        The text content of the LLM response.

    Raises:
        ValueError: If the LLM returns no content.
    """
    effective_model = model or DEFAULT_MODEL
    client = LLMClientFactory.create_client({"model": effective_model})

    messages: list[SystemMessage | UserMessage] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(UserMessage(content=prompt))

    result = await client.generate_response(messages)

    if not result.content:
        raise ValueError("LLM returned no content")

    return result.content


async def llm_call_json_async(
    prompt: str,
    # ast-grep-ignore: no-dict-any - JSON schema is genuinely arbitrary
    schema: dict[str, Any] | None = None,
    system: str | None = None,
    model: str | None = None,
    # ast-grep-ignore: no-dict-any - JSON output structure is determined by LLM
) -> dict[str, Any] | list[Any]:
    """Make a one-shot LLM call and return parsed JSON.

    Args:
        prompt: The user prompt to send.
        schema: Optional JSON schema to include in the system prompt.
        system: Optional additional system message.
        model: Model identifier (default: gemini-3-flash-preview).

    Returns:
        Parsed JSON as a dict or list.

    Raises:
        ValueError: If the LLM returns no content or invalid JSON.
    """
    json_system = "Respond with valid JSON only. No markdown, no explanation."
    if schema:
        json_system += f"\n\nExpected JSON schema:\n{json.dumps(schema, indent=2)}"
    if system:
        json_system += f"\n\n{system}"

    raw = await llm_call_async(prompt, system=json_system, model=model)
    cleaned = _strip_markdown_fences(raw)
    return json.loads(cleaned)  # type: ignore[no-any-return]
