"""LLM API for the scripting engine.

Provides llm() and llm_json() functions for scripts to make one-shot
LLM calls for summarisation, data extraction, and similar tasks.
"""

import json
import logging
from typing import Any, cast

from family_assistant.llm.one_shot import DEFAULT_MODEL, one_shot, one_shot_json

logger = logging.getLogger(__name__)

# Re-export DEFAULT_MODEL for tests that import from this module
__all__ = ["DEFAULT_MODEL", "llm_call_async", "llm_call_json_async"]


async def llm_call_async(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
) -> str:
    """Make a one-shot LLM call and return the text response.

    Args:
        prompt: The user prompt to send.
        system: Optional system message.
        model: Model identifier (default: gemini-3.5-flash).

    Returns:
        The text content of the LLM response.

    Raises:
        ValueError: If the LLM returns no content.
    """
    return await one_shot(prompt, system=system, model=model or DEFAULT_MODEL)


async def llm_call_json_async(
    prompt: str,
    # ast-grep-ignore: no-dict-any - JSON schema is genuinely arbitrary
    schema: dict[str, Any] | None = None,
    system: str | None = None,
    model: str | None = None,
    # ast-grep-ignore: no-dict-any - JSON output structure is determined by LLM
) -> dict[str, Any]:
    """Make a one-shot LLM call and return parsed JSON object.

    Uses native JSON mode when available (OpenAI, Gemini) for more
    reliable JSON output.

    Args:
        prompt: The user prompt to send.
        schema: Optional JSON schema to include in the system prompt.
        system: Optional additional system message.
        model: Model identifier (default: gemini-3.5-flash).

    Returns:
        Parsed JSON object (dict).

    Raises:
        StructuredOutputError: If response cannot be parsed as JSON.
    """
    combined_system: str | None = None
    if schema or system:
        parts = []
        if schema:
            parts.append(f"Expected JSON schema:\n{json.dumps(schema, indent=2)}")
        if system:
            parts.append(system)
        combined_system = "\n\n".join(parts)

    result = await one_shot_json(
        prompt, system=combined_system, model=model or DEFAULT_MODEL
    )
    return cast("dict[str, Any]", result)
