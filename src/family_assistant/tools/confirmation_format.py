"""Building blocks for confirmation prompts, shared by core and plugin tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolArgumentsView, ToolExecutionContext


def markdown_code_block(text: str) -> str:
    """Render text as inert markdown using a fence longer than any content fence."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def confirmation_value(value: object) -> str:
    """Render a value for a confirmation prompt.

    Never truncates: an approver must see the whole payload they are approving,
    and whether it can be displayed is the delivering interface's call, not a
    renderer's (see docs/design/confirmation-prompt-capacity.md).
    """
    return "" if value is None else str(value)


def confirmation_field(label: str, value: object) -> str:
    """Format a single confirmation field."""
    return f"- {label}:\n{markdown_code_block(confirmation_value(value))}"


class ConfirmationRenderer(Protocol):
    """Protocol for confirmation prompt renderers.

    Confirmation renderers are responsible for fetching any necessary data
    and formatting a human-readable confirmation prompt. They receive the
    full ToolExecutionContext to access configuration, timezone, etc.
    """

    async def __call__(
        self,
        args: ToolArgumentsView,
        context: ToolExecutionContext,
    ) -> str:
        """Render a confirmation prompt from tool arguments.

        Args:
            args: Tool arguments (e.g., uid, calendar_url for calendar tools)
            context: Execution context with timezone, calendar config, etc.

        Returns:
            Formatted confirmation prompt string
        """
        ...
