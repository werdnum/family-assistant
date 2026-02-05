"""LLM API for Starlark scripts.

This module provides one-shot LLM call functions for Starlark scripts,
enabling summarization, data extraction, and text generation within
the scripting environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import SystemMessage, UserMessage

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3-flash-preview"


class LlmAPI:
    """Provides LLM access to Starlark scripts via async-sync bridge."""

    def __init__(
        self,
        main_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._main_loop = main_loop

    def _run_async(self, coro: Any) -> Any:  # noqa: ANN401
        """Run an async coroutine from sync Starlark context."""
        if self._main_loop and self._main_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
            return future.result(timeout=120.0)

        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=120.0)
        except RuntimeError:
            return asyncio.run(coro)

    def call(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
    ) -> str:
        """Make a one-shot LLM call and return the text response.

        Args:
            prompt: The user prompt to send to the LLM.
            system: Optional system message to set context.
            model: Model to use (default: gemini-3-flash-preview).

        Returns:
            The LLM's text response.
        """
        return self._run_async(self._call_async(prompt, system, model))

    async def _call_async(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
    ) -> str:
        """Async implementation of call."""
        client = LLMClientFactory.create_client({"model": model or DEFAULT_MODEL})

        messages: list[SystemMessage | UserMessage] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(UserMessage(content=prompt))

        result = await client.generate_response(messages)

        if result.content is None:
            raise ValueError("LLM returned no content")

        return result.content

    def call_json(
        self,
        prompt: str,
        # ast-grep-ignore: no-dict-any - Schema is arbitrary JSON schema dict
        schema: dict[str, Any] | None = None,
        system: str | None = None,
        model: str | None = None,
        # ast-grep-ignore: no-dict-any - Return dict for Starlark JSON compatibility
    ) -> dict[str, Any] | list[Any]:
        """Make a one-shot LLM call and return parsed JSON.

        Args:
            prompt: The user prompt to send to the LLM.
            schema: Optional JSON schema dict describing expected response structure.
            system: Optional system message to set context.
            model: Model to use (default: gemini-3-flash-preview).

        Returns:
            Parsed JSON response as a dict or list.
        """
        return self._run_async(self._call_json_async(prompt, schema, system, model))

    async def _call_json_async(
        self,
        prompt: str,
        # ast-grep-ignore: no-dict-any - Schema is arbitrary JSON schema dict
        schema: dict[str, Any] | None = None,
        system: str | None = None,
        model: str | None = None,
        # ast-grep-ignore: no-dict-any - Return dict for Starlark JSON compatibility
    ) -> dict[str, Any] | list[Any]:
        """Async implementation of call_json."""
        client = LLMClientFactory.create_client({"model": model or DEFAULT_MODEL})

        schema_instruction = (
            "You must respond with valid JSON only, no additional text or markdown."
        )
        if schema:
            schema_instruction = (
                "You must respond with valid JSON that matches this schema:\n"
                f"```json\n{json.dumps(schema, indent=2)}\n```\n\n"
                "Respond ONLY with the JSON object, no additional text or markdown."
            )

        combined_system = schema_instruction
        if system:
            combined_system = f"{system}\n\n{schema_instruction}"

        messages: list[SystemMessage | UserMessage] = [
            SystemMessage(content=combined_system),
            UserMessage(content=prompt),
        ]

        result = await client.generate_response(messages)

        if result.content is None:
            raise ValueError("LLM returned no content")

        raw = result.content.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = lines[1:]  # Remove opening ```json
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines)

        return json.loads(raw)


def create_llm_api(
    main_loop: asyncio.AbstractEventLoop | None = None,
) -> LlmAPI:
    """Create an LlmAPI instance for use in Starlark scripts.

    Args:
        main_loop: Main event loop for async operations.

    Returns:
        LlmAPI instance.
    """
    return LlmAPI(main_loop=main_loop)
