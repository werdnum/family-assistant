"""Note management tools.

This module contains tools for creating, updating, and managing notes
that can be included in the assistant's context.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


async def add_or_update_note_tool(
    exec_context: ToolExecutionContext,
    title: str,
    content: str,
    include_in_prompt: bool = True,
    append: bool = False,
    attachment_ids: list[str] | None = None,
    visibility_labels: list[str] | None = None,
) -> str:
    """
    Adds a new note or updates an existing note with the given title.

    Args:
        exec_context: The execution context
        title: The title of the note
        content: The content of the note
        include_in_prompt: Whether to include the note in system prompts
        append: Whether to append to existing content instead of replacing it
        attachment_ids: Optional list of attachment UUIDs to associate with this note
        visibility_labels: Optional list of visibility labels for access control.
            If not specified, new notes get default labels from config.

    Returns:
        A string indicating success or failure
    """
    db_context = exec_context.db_context
    attachment_registry = exec_context.attachment_registry

    # Enforce visibility: check if user can see existing note before allowing update
    labels_to_use = visibility_labels
    visible_existing = await db_context.notes.get_by_title(
        title, visibility_grants=exec_context.visibility_grants
    )
    if visible_existing is None:
        # Check if title is taken by a note the user can't see
        any_existing = await db_context.notes.get_by_title(
            title, visibility_grants=None
        )
        if any_existing:
            return f"Error: Cannot modify note '{title}' - insufficient visibility permissions."
        # Truly new note - apply default labels if none specified
        if labels_to_use is None and exec_context.default_note_visibility_labels:
            labels_to_use = exec_context.default_note_visibility_labels

    # Validate attachment IDs if provided
    # None means "preserve existing", empty list means "clear all attachments"
    valid_attachment_ids: list[str] | None = None
    if attachment_ids is not None:
        valid_attachment_ids = []
        for attachment_id in attachment_ids:
            if attachment_registry:
                # Verify attachment exists
                metadata = await attachment_registry.get_attachment(
                    db_context, attachment_id
                )
                if metadata:
                    valid_attachment_ids.append(attachment_id)
                else:
                    logger.warning(
                        f"Attachment {attachment_id} not found, skipping in note '{title}'"
                    )
            else:
                # No registry available, log warning but allow the ID
                logger.warning(
                    f"AttachmentRegistry not available, cannot validate attachment {attachment_id}"
                )
                valid_attachment_ids.append(attachment_id)

    try:
        result = await db_context.notes.add_or_update(
            title=title,
            content=content,
            include_in_prompt=include_in_prompt,
            append=append,
            attachment_ids=valid_attachment_ids,  # None preserves existing, [] clears
            visibility_labels=labels_to_use,
        )
        attachment_info = (
            f" with {len(valid_attachment_ids)} attachment(s)"
            if valid_attachment_ids
            else ""
        )
        return f"Note '{title}' has been {'updated' if result == 'Success' else 'created'} successfully{attachment_info}."
    except Exception as e:
        logger.error(f"Error adding/updating note '{title}': {e}", exc_info=True)
        return f"Error: Failed to add/update note '{title}'. {e}"


# Tool Definitions
NOTE_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "add_or_update_note",
            "description": (
                "Add a new note or update an existing note with the given title. Use this to remember information provided by the user. "
                "You can control whether the note appears in your system prompt with the include_in_prompt parameter. "
                "Notes can have attachments (images, documents) associated with them by providing attachment UUIDs.\n\n"
                "Returns: A string indicating the operation result. "
                "On success, returns 'Note [title] has been [created/updated] successfully.'. "
                "On error, returns 'Error: Failed to add/update note [title]. [error details]'."
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
                    "append": {
                        "type": "boolean",
                        "description": "Whether to append the content to an existing note instead of replacing it. Default is false. When true, the content will be added to the end of the existing note with a newline separator.",
                        "default": False,
                    },
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of attachment UUIDs to associate with this note. These attachments will be returned when retrieving the note.",
                    },
                    "visibility_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of visibility labels for access control. Notes are only visible to profiles with matching grants. If not specified, new notes get default labels from config. Use an empty list [] to make a note visible to all profiles.",
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
                "Retrieve a specific note by its title to check its content, prompt inclusion status, and attachments. "
                "Returns the note's title, content, whether it's included in the system prompt, and any associated attachments.\n\n"
                "Returns: A JSON string containing a dict with the note information. "
                "If note exists, returns {'exists': true, 'title': [title], 'content': [full content], 'include_in_prompt': [boolean], 'attachment_count': [integer]}. "
                "If note not found, returns {'exists': false, 'title': [title], 'content': null, 'include_in_prompt': null, 'attachment_count': 0}. "
                "Attachments are returned as multimodal content that vision models can see."
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
                "List all notes with their titles, prompt inclusion status, and attachment counts. "
                "Can optionally filter to show only notes that are included or excluded from the system prompt.\n\n"
                "Returns: A JSON string containing a list of note summaries. "
                "Returns an array where each item is {'title': [title], 'include_in_prompt': [boolean], 'content_preview': [first 100 chars], 'attachment_count': [integer]}. "
                "If no notes exist or match the filter, returns an empty array '[]'."
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
                "Use with caution as this action cannot be undone.\n\n"
                "Returns: A JSON string containing a dict with the operation result. "
                "On success, returns {'success': true, 'message': 'Note [title] deleted successfully.'}. "
                "If note not found, returns {'success': false, 'message': 'Note [title] not found.'}."
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
    title: str,
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Tool wrapper for get_note_by_title with attachment support."""
    db_context = exec_context.db_context
    attachment_registry = exec_context.attachment_registry

    note = await db_context.notes.get_by_title(
        title, visibility_grants=exec_context.visibility_grants
    )
    if not note:
        # Fall back to file-based skills via NoteRegistry
        if exec_context.note_registry:
            skill = exec_context.note_registry.get_skill_by_name(
                title, visibility_grants=exec_context.visibility_grants
            )
            if skill:
                return ToolResult(
                    data={
                        "exists": True,
                        "title": skill.name,
                        "content": skill.content,
                        "include_in_prompt": False,
                        "attachment_count": 0,
                        "source": "file",
                    }
                )

        return ToolResult(
            data={
                "exists": False,
                "title": title,
                "content": None,
                "include_in_prompt": None,
                "attachment_count": 0,
            }
        )

    # Parse attachment_ids from the note
    attachment_ids_raw = note.attachment_ids
    attachment_ids: list[str] = []
    if attachment_ids_raw:
        if isinstance(attachment_ids_raw, str):
            # Parse JSON string
            try:
                attachment_ids = json.loads(attachment_ids_raw)
            except json.JSONDecodeError:
                logger.warning(
                    f"Failed to parse attachment_ids for note '{title}': {attachment_ids_raw}"
                )
        elif isinstance(attachment_ids_raw, list):
            attachment_ids = attachment_ids_raw

    # Prepare result data
    result_data = {
        "exists": True,
        "title": note.title,
        "content": note.content,
        "include_in_prompt": note.include_in_prompt,
        "attachment_count": len(attachment_ids),
    }

    # Fetch attachment metadata and content
    attachments: list[ToolAttachment] = []
    if attachment_ids and attachment_registry:
        for attachment_id in attachment_ids:
            try:
                metadata = await attachment_registry.get_attachment(
                    db_context, attachment_id
                )
                if metadata:
                    # Fetch content
                    content = await attachment_registry.get_attachment_content(
                        db_context, attachment_id
                    )
                    if content:
                        attachments.append(
                            ToolAttachment(
                                mime_type=metadata.mime_type,
                                content=content,
                                description=metadata.description,
                                attachment_id=attachment_id,
                            )
                        )
                    else:
                        logger.warning(
                            f"Could not fetch content for attachment {attachment_id}"
                        )
                else:
                    logger.warning(
                        f"Attachment {attachment_id} referenced in note '{title}' not found"
                    )
            except Exception as e:
                logger.error(
                    f"Error fetching attachment {attachment_id} for note '{title}': {e}",
                    exc_info=True,
                )

    return ToolResult(
        data=result_data, attachments=attachments if attachments else None
    )


async def list_notes_tool(
    exec_context: ToolExecutionContext,
    include_in_prompt: bool | None = None,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
) -> list[dict[str, Any]]:
    """Tool wrapper for get_all_notes with optional filtering."""
    all_notes = await exec_context.db_context.notes.get_all(
        visibility_grants=exec_context.visibility_grants
    )

    # Apply filtering if requested
    if include_in_prompt is not None:
        filtered_notes = [
            note for note in all_notes if note.include_in_prompt == include_in_prompt
        ]
    else:
        filtered_notes = all_notes

    # Return summary with attachment count
    return [
        {
            "title": note.title,
            "include_in_prompt": note.include_in_prompt,
            "content_preview": note.content[:100] + "..."
            if len(note.content) > 100
            else note.content,
            "attachment_count": len(note.attachment_ids),
        }
        for note in filtered_notes
    ]


async def delete_note_tool(
    title: str,
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
) -> dict[str, Any]:
    """Tool wrapper for delete_note."""
    # Enforce visibility: only allow deleting notes the user can see
    visible = await exec_context.db_context.notes.get_by_title(
        title, visibility_grants=exec_context.visibility_grants
    )
    if not visible:
        return {
            "success": False,
            "message": f"Note '{title}' not found.",
        }
    deleted = await exec_context.db_context.notes.delete(title)
    return {
        "success": deleted,
        "message": f"Note '{title}' deleted successfully."
        if deleted
        else f"Note '{title}' not found.",
    }
