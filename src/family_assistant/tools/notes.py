"""Note management tools.

This module contains tools for creating, updating, and managing notes
that can be included in the assistant's context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


async def add_or_update_note_tool(
    exec_context: ToolExecutionContext,
    title: str,
    content: str,
    include_in_prompt: bool = True,
) -> str:
    """
    Adds a new note or updates an existing note with the given title.

    Args:
        exec_context: The execution context
        title: The title of the note
        content: The content of the note
        include_in_prompt: Whether to include the note in system prompts

    Returns:
        A string indicating success or failure
    """
    db_context = exec_context.db_context
    try:
        result = await db_context.notes.add_or_update(
            title=title,
            content=content,
            include_in_prompt=include_in_prompt,
        )
        return f"Note '{title}' has been {'updated' if result == 'Success' else 'created'} successfully."
    except Exception as e:
        logger.error(f"Error adding/updating note '{title}': {e}", exc_info=True)
        return f"Error: Failed to add/update note '{title}'. {e}"


# Tool Definitions
NOTE_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "add_or_update_note",
            "description": (
                "Add a new note or update an existing note with the given title. Use this to remember information provided by the user. "
                "You can control whether the note appears in your system prompt with the include_in_prompt parameter."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The unique title of the note.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content of the note.",
                    },
                    "include_in_prompt": {
                        "type": "boolean",
                        "description": "Whether to include this note in the system prompt context. Default is true. Set to false for notes that should be searchable but not always visible.",
                        "default": True,
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_note",
            "description": (
                "Retrieve a specific note by its title to check its content and prompt inclusion status. "
                "Returns the note's title, content, and whether it's included in the system prompt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The title of the note to retrieve.",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": (
                "List all notes with their titles and prompt inclusion status. "
                "Can optionally filter to show only notes that are included or excluded from the system prompt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_in_prompt": {
                        "type": "boolean",
                        "description": "Optional filter. If true, shows only notes included in prompt. If false, shows only excluded notes. If not specified, shows all notes.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": (
                "Delete a note by its title. This permanently removes the note from the system. "
                "Use with caution as this action cannot be undone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The title of the note to delete.",
                    },
                },
                "required": ["title"],
            },
        },
    },
]


# Tool Implementations
# Note: The actual implementations are in the storage module.
# We need to create wrapper functions that match the tool signatures.


async def get_note_tool(
    title: str, exec_context: ToolExecutionContext
) -> dict[str, Any]:
    """Tool wrapper for get_note_by_title."""
    note = await exec_context.db_context.notes.get_by_title(title)
    if note:
        return {
            "exists": True,
            "title": note["title"],
            "content": note["content"],
            "include_in_prompt": note["include_in_prompt"],
        }
    else:
        return {
            "exists": False,
            "title": title,
            "content": None,
            "include_in_prompt": None,
        }


async def list_notes_tool(
    exec_context: ToolExecutionContext, include_in_prompt: bool | None = None
) -> list[dict[str, Any]]:
    """Tool wrapper for get_all_notes with optional filtering."""
    all_notes = await exec_context.db_context.notes.get_all()

    # Apply filtering if requested
    if include_in_prompt is not None:
        filtered_notes = [
            note for note in all_notes if note["include_in_prompt"] == include_in_prompt
        ]
    else:
        filtered_notes = all_notes

    # Return summary without full content for list view
    return [
        {
            "title": note["title"],
            "include_in_prompt": note["include_in_prompt"],
            "content_preview": note["content"][:100] + "..."
            if len(note["content"]) > 100
            else note["content"],
        }
        for note in filtered_notes
    ]


async def delete_note_tool(
    title: str, exec_context: ToolExecutionContext
) -> dict[str, Any]:
    """Tool wrapper for delete_note."""
    deleted = await exec_context.db_context.notes.delete(title)
    return {
        "success": deleted,
        "message": f"Note '{title}' deleted successfully."
        if deleted
        else f"Note '{title}' not found.",
    }
