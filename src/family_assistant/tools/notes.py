"""Note management tools.

This module contains tools for creating, updating, and managing notes
that can be included in the assistant's context.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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


async def get_note_tool(title: str, context: Any) -> dict[str, Any]:
    """Tool wrapper for get_note_by_title."""
    from family_assistant import storage

    note = await storage.get_note_by_title(context.db_context, title)
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
    include_in_prompt: bool | None = None, context: Any = None
) -> list[dict[str, Any]]:
    """Tool wrapper for get_all_notes with optional filtering."""
    from family_assistant import storage

    all_notes = await storage.get_all_notes(context.db_context)

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


async def delete_note_tool(title: str, context: Any) -> dict[str, Any]:
    """Tool wrapper for delete_note."""
    from family_assistant import storage

    deleted = await storage.delete_note(context.db_context, title)
    return {
        "success": deleted,
        "message": f"Note '{title}' deleted successfully."
        if deleted
        else f"Note '{title}' not found.",
    }
