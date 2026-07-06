"""Note management tools.

This module contains tools for creating, updating, and managing notes
that can be included in the assistant's context.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, TypedDict

from family_assistant.security.taint import (
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class NoteProvenanceMetadata(TypedDict):
    """Stored note provenance metadata owned by note writes."""

    taint_metadata: TaintMetadata
    provenance_labels: list[str]


_TAINT_LABELS_BY_TIER: dict[SourceTrustTier, str] = {
    SourceTrustTier.KNOWN_CONTACT: "source_known_contact",
    SourceTrustTier.RECOGNIZED_MACHINE: "source_recognized_machine",
    SourceTrustTier.UNKNOWN_EXTERNAL: "source_unknown_external",
}


def _note_provenance_from_taint(
    exec_context: ToolExecutionContext,
) -> NoteProvenanceMetadata | None:
    """Return durable provenance metadata for the current turn taint."""
    if exec_context.taint_tracker is None:
        return None
    state = exec_context.taint_tracker.snapshot()
    label = _TAINT_LABELS_BY_TIER.get(state.max_tier)
    if label is None:
        return None
    return {"taint_metadata": state.to_metadata(), "provenance_labels": [label]}


async def add_or_update_note_tool(
    exec_context: ToolExecutionContext,
    title: str,
    content: str,
    include_in_prompt: bool = False,
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
    # Local import: the notes repository transitively imports the tools package
    # (repositories/__init__ -> schedule_automations -> task_worker -> tools),
    # so a top-level import here would be circular.
    from family_assistant.storage.repositories.notes import (  # noqa: PLC0415
        NoteWritePolicyError,
    )

    db_context = exec_context.db_context
    attachment_registry = exec_context.attachment_registry

    # Visibility confinement (see-before-overwrite, default/required/allowed
    # labels) is enforced in the repository via the write policy so every write
    # path is covered, not just this tool.
    write_policy = exec_context.note_write_policy()

    provenance_metadata = _note_provenance_from_taint(exec_context)

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
            visibility_labels=visibility_labels,
            write_policy=write_policy,
            provenance_metadata=provenance_metadata,
        )
        attachment_info = (
            f" with {len(valid_attachment_ids)} attachment(s)"
            if valid_attachment_ids
            else ""
        )
        return f"Note '{title}' has been {'updated' if result == 'Success' else 'created'} successfully{attachment_info}."
    except NoteWritePolicyError as e:
        return f"Error: {e}"
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
                "Notes can have attachments (images, documents) associated with them by providing attachment UUIDs. "
                "Leave `include_in_prompt` at its default `false` unless the note is short, evergreen context that must load every "
                "turn (see the parameter description). To create a reusable skill instead of a plain note, load the 'Skill Creation' "
                "skill via `get_note` for the frontmatter format.\n\n"
                "Returns a string indicating success or an error message."
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
                        "description": "Whether to auto-load the full note into every system prompt. Default is false — the note is still stored, searchable, and its title is listed in the system prompt so you can load it on demand via `get_note`. Set to true ONLY for short evergreen context (durable user preferences, household policies, persistent identity facts) that you want present every turn.",
                        "default": False,
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
                        "description": "Optional list of visibility labels for access control. Notes are only visible to profiles with matching grants. If not specified, new notes get default labels from config. The active profile's policy may add required labels or reject label values you request. An empty list [] only makes the note visible to all profiles when the active profile permits unrestricted note writes.",
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
                result_data = {
                    "exists": True,
                    "title": skill.name,
                    "content": skill.content,
                    "include_in_prompt": False,
                    "attachment_count": 0,
                    "source": "file",
                }
                if skill.activate_tools:
                    result_data["activate_tools"] = list(skill.activate_tools)
                if skill.activate_mcp_servers:
                    result_data["activate_mcp_servers"] = list(
                        skill.activate_mcp_servers
                    )
                return ToolResult(data=result_data)

        return ToolResult(
            data={
                "exists": False,
                "title": title,
                "content": None,
                "include_in_prompt": None,
                "attachment_count": 0,
            }
        )

    provenance_metadata = note.provenance_metadata
    if exec_context.taint_tracker is not None and isinstance(provenance_metadata, dict):
        taint_metadata = provenance_metadata.get("taint_metadata")
        note_taint_state = TurnTaintState.from_metadata(taint_metadata)
        if note_taint_state.max_tier > SourceTrustTier.TRUSTED_USER:
            exec_context.taint_tracker.add_source(
                TaintSource(
                    source_type=TaintSourceType.NOTE,
                    source_id=note.title,
                    tier=note_taint_state.max_tier,
                    labels=frozenset(note.visibility_labels),
                    reason=f"Note '{note.title}' carries stored provenance taint.",
                )
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
        "provenance_labels": note.visibility_labels,
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
    # ast-grep-ignore: no-dict-any - note summary dict has mixed value types
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
    # ast-grep-ignore: no-dict-any - tool result dict has mixed value types
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
