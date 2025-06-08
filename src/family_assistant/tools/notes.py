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
]


# Tool Implementations
# Note: The actual implementation is in storage module
# We'll import it when building AVAILABLE_FUNCTIONS in __init__.py
