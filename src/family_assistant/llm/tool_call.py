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
    arguments: str  # JSON string of arguments


@dataclass(frozen=True)
class ToolCallItem:
    """Represents a single tool call requested by the LLM."""

    id: str
    type: str  # Usually "function"
    function: ToolCallFunction
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    provider_metadata: dict[str, Any] | None = (
        None  # Provider-specific metadata (e.g., thought signatures)
    )
