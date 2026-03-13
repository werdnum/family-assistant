"""LLM API for the scripting engine.

Provides llm() and llm_json() functions for scripts to make one-shot
LLM calls for summarisation, data extraction, and similar tasks.
"""

import json
import logging
import re
from typing import Any

from family_assistant.llm.one_shot import DEFAULT_MODEL, one_shot

logger = logging.getLogger(__name__)

# Re-export DEFAULT_MODEL for tests that import from this module
__all__ = ["DEFAULT_MODEL", "llm_call_async", "llm_call_json_async"]


def extract_json_from_response(text: str) -> str:
    """Extract JSON from LLM response, handling markdown fences and conversational text.

    Handles cases like:
    - Pure JSON: {"key": "value"}
    - Fenced JSON: ```json\n{"key": "value"}\n```
    - Conversational: "Here is the JSON:\n```json\n{"key": "value"}\n```"
    """
    # First try to find JSON in markdown fences (handles conversational text before fences)
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Fall back to stripping fences at start/end (for simple cases)
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
    return await one_shot(prompt, system=system, model=model or DEFAULT_MODEL)


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
        ValueError: If the LLM returns no content, invalid JSON, or a scalar JSON value.
    """
    json_system = "Respond with valid JSON only. No markdown, no explanation."
    if schema:
        json_system += f"\n\nExpected JSON schema:\n{json.dumps(schema, indent=2)}"
    if system:
        json_system += f"\n\n{system}"

    raw = await llm_call_async(prompt, system=json_system, model=model)
    cleaned = extract_json_from_response(raw)
    result = json.loads(cleaned)

    # Validate that result is dict or list per type contract
    if not isinstance(result, (dict, list)):
        raise ValueError(
            f"Expected JSON object or array, got {type(result).__name__}: {result!r}"
        )

    return result  # type: ignore[no-any-return]  # json.loads returns Any, narrowed by isinstance above
