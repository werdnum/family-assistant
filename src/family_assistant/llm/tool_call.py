"""
Tool call data structures.

These classes represent tool/function calls made by the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCallFunction:
    """Represents the function to be called in a tool call."""

    name: str
    arguments: str | dict[str, object]  # JSON string or structured args


@dataclass(frozen=True)
class ToolCallItem:
    """Represents a single tool call requested by the LLM."""

    id: str
    type: str  # Usually "function"
    function: ToolCallFunction
    # ast-grep-ignore: no-dict-any - Accepts both dicts (for serialization) and provider metadata objects (e.g., GeminiProviderMetadata)
    provider_metadata: Any | None = (
        None  # Provider-specific metadata (e.g., thought signatures)
    )
