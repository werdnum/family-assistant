"""Note management tools.

This module contains tools for creating, updating, and managing notes
that can be included in the assistant's context.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from family_assistant.memory.invariants import MemoryWriteError
from family_assistant.security.ambient_admission import (
    AdmissionOutcome,
    AmbientAdmissionDecision,
    AmbientCandidate,
    CandidateAttachment,
    is_external_candidate,
)
from family_assistant.security.note_provenance import (
    NoteProvenanceStamp,
    note_read_taint,
    stored_note_state,
)
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    artifact_taint_sources,
    merge_taint_state_into_tracker,
    merge_taint_states,
)
from family_assistant.tools.taint_helpers import merge_artifact_taint_into_context
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.database import Database
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


async def _load_note_attachment(
    exec_context: ToolExecutionContext,
    attachment_registry: AttachmentRegistry,
    db_context: Database,
    attachment_id: str,
    *,
    user_id: str | None,
    note_title: str,
) -> ToolAttachment | None:
    metadata = await attachment_registry.get_attachment(
        db_context, attachment_id, acting_user_id=user_id
    )
    if metadata is None:
        logger.warning(
            f"Attachment {attachment_id} referenced in note '{note_title}' not found"
        )
        return None

    content = await attachment_registry.get_attachment_content(
        db_context, attachment_id, acting_user_id=user_id
    )
    if not content:
        logger.warning(f"Could not fetch content for attachment {attachment_id}")
        return None
    # A reviewed note vouches for its attachments' descriptions, not their
    # contents: each attachment returned brings its own provenance.
    merge_artifact_taint_into_context(
        exec_context,
        provenance_metadata=metadata.metadata,
        fallback_source_type=TaintSourceType.ATTACHMENT,
        fallback_source_id=attachment_id,
        fallback_reason=f"Attachment of note '{note_title}' carries stored provenance.",
    )
    return ToolAttachment(
        mime_type=metadata.mime_type,
        content=content,
        description=metadata.description,
        attachment_id=attachment_id,
    )


def note_stamp_from_context(exec_context: ToolExecutionContext) -> NoteProvenanceStamp:
    """The provenance stamp for a note the model composed in this turn.

    Shared with the memory apply path. The repository floors it at
    ``trusted_internal`` -- model output is never the human's own words -- and
    merges whatever the write retains from the stored note.
    """
    state = (
        exec_context.taint_tracker.snapshot()
        if exec_context.taint_tracker is not None
        else TurnTaintState.empty()
    )
    return NoteProvenanceStamp.machine(state)


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
        include_in_prompt: Whether to auto-load the note into the assistant's
            per-turn context
        append: Whether to append to existing content instead of replacing it
        attachment_ids: Optional list of attachment UUIDs to associate with this note
        visibility_labels: Optional list of visibility labels for access control.
            If not specified, new notes get default labels from config.

    Returns:
        A string indicating success or failure
    """
    db_context = exec_context.db_context
    attachment_registry = exec_context.attachment_registry

    # Validate attachment IDs if provided
    # None means "preserve existing", empty list means "clear all attachments"
    valid_attachment_ids: list[str] | None = None
    if attachment_ids is not None:
        valid_attachment_ids = []
        for attachment_id in attachment_ids:
            if attachment_registry:
                # Verify attachment exists
                metadata = await attachment_registry.get_attachment(
                    db_context, attachment_id, acting_user_id=exec_context.user_id
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

    outcome = await write_note_through_admission(
        exec_context,
        tool_name="add_or_update_note",
        title=title,
        content=content,
        include_in_prompt=include_in_prompt,
        append=append,
        attachment_ids=valid_attachment_ids,
        visibility_labels=visibility_labels,
    )
    if outcome.error is not None:
        return f"Error: {outcome.error}"
    attachment_info = (
        f" with {len(valid_attachment_ids)} attachment(s)"
        if valid_attachment_ids
        else ""
    )
    verb = "created" if outcome.created else "updated"
    message = f"Note '{title}' has been {verb} successfully{attachment_info}."
    if outcome.admission_note:
        message += f" {outcome.admission_note}"
    return message


@dataclass(frozen=True)
class NoteWriteOutcome:
    """What a note write through the admission gate did."""

    error: str | None = None
    created: bool = False
    admission: AdmissionOutcome | None = None
    admission_note: str | None = None
    """A sentence for the tool result when the write was gated."""


@dataclass(frozen=True)
class _ResolvedWrite:
    candidate: AmbientCandidate
    revision: str
    gate_state: TurnTaintState
    ambient: bool


async def _resolve_candidate(
    exec_context: ToolExecutionContext,
    *,
    title: str,
    content: str,
    include_in_prompt: bool,
    append: bool,
    attachment_ids: list[str] | None,
    imported_from: str | None,
) -> _ResolvedWrite:
    """Resolve a write into the complete note it would persist.

    The gate's tier is the maximum of the turn (floored at ``trusted_internal``),
    everything the candidate retains from the stored note -- the title always,
    so any update is at least the stored tier -- and every attachment whose
    metadata the candidate renders. An attachment with no stored envelope is
    evaluated as ``unknown_external`` here: its description is about to reach
    every prompt, and an unlabelled artifact must not pass on the strength of
    what nobody recorded.
    """
    # Local import: see add_or_update_note_tool.
    from family_assistant.storage.repositories.notes import (  # noqa: PLC0415
        detect_skill_metadata,
        note_revision,
    )

    db_context = exec_context.db_context
    existing = await db_context.notes.get_by_title(
        title,
        read_policy=exec_context.note_write_policy().see_before_overwrite_read_policy(),
    )
    states: list[TurnTaintState] = [
        (
            exec_context.taint_tracker.snapshot()
            if exec_context.taint_tracker is not None
            else TurnTaintState.empty()
        ).with_authorship_floor()
    ]
    if existing is not None:
        states.append(
            stored_note_state(existing.provenance_metadata, title=existing.title)
        )
    resolved_content = (
        f"{existing.content}\n{content}" if append and existing is not None else content
    )
    resolved_attachment_ids = (
        attachment_ids
        if attachment_ids is not None
        else (existing.attachment_ids if existing is not None else [])
    )
    attachments: list[CandidateAttachment] = []
    registry = exec_context.attachment_registry
    for attachment_id in resolved_attachment_ids:
        metadata = (
            await registry.get_attachment(
                db_context, attachment_id, acting_user_id=exec_context.user_id
            )
            if registry is not None
            else None
        )
        attachments.append(
            CandidateAttachment(
                attachment_id=attachment_id,
                description=metadata.description if metadata is not None else None,
                mime_type=metadata.mime_type if metadata is not None else None,
            )
        )
        sources = artifact_taint_sources(
            metadata.metadata if metadata is not None else None,
            source_id=attachment_id,
        )
        if not sources:
            sources = (
                TaintSource(
                    source_type=TaintSourceType.ATTACHMENT,
                    source_id=attachment_id,
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason="Attachment has no stored provenance envelope.",
                ),
            )
        attachment_state = TurnTaintState.empty()
        for source in sources:
            attachment_state = attachment_state.add_source(source)
        states.append(attachment_state)
    is_skill, skill_name, skill_description = detect_skill_metadata(resolved_content)
    return _ResolvedWrite(
        candidate=AmbientCandidate(
            title=title,
            content=resolved_content,
            include_in_prompt=include_in_prompt,
            is_skill=is_skill,
            skill_name=skill_name,
            skill_description=skill_description,
            attachments=tuple(attachments),
            imported_from=imported_from,
        ),
        revision=note_revision(existing),
        gate_state=merge_taint_states(*states),
        ambient=include_in_prompt or is_skill,
    )


async def _admission_decision(
    exec_context: ToolExecutionContext,
    resolved: _ResolvedWrite,
    *,
    tool_name: str,
) -> AmbientAdmissionDecision:
    if not resolved.ambient:
        return AmbientAdmissionDecision(
            outcome=AdmissionOutcome.NOT_GATED,
            reason="The note is reference material, not ambient.",
        )
    # Local import: the infrastructure module imports the tools package.
    from family_assistant.tools.infrastructure import (  # noqa: PLC0415
        TaintTrackingToolsProvider,
        find_provider_by_type,
    )

    provider = exec_context.tools_provider
    if provider is None and exec_context.processing_service is not None:
        provider = exec_context.processing_service.tools_provider
    gate = (
        find_provider_by_type(provider, TaintTrackingToolsProvider)
        if provider is not None
        else None
    )
    if gate is None:
        if is_external_candidate(resolved.gate_state.max_tier):
            logger.warning(
                "No runtime taint policy is reachable for ambient write of %r; "
                "persisting it as reference material.",
                resolved.candidate.title,
            )
            return AmbientAdmissionDecision(
                outcome=AdmissionOutcome.NOT_ADMITTED,
                reason="No admission review is available in this context.",
            )
        return AmbientAdmissionDecision(
            outcome=AdmissionOutcome.NOT_GATED,
            reason="No external material to admit.",
        )
    return await gate.adjudicate_ambient_admission(
        candidate=resolved.candidate,
        state=resolved.gate_state,
        context=exec_context,
        tool_name=tool_name,
        call_id=exec_context.tool_call_id,
    )


def _stamp_for(
    resolved: _ResolvedWrite,
    decision: AmbientAdmissionDecision,
    exec_context: ToolExecutionContext,
) -> NoteProvenanceStamp:
    """The stamp a gated (or ungated) write persists with.

    Only an admitting decision promotes an external candidate, and it replaces
    the envelope. A non-admitted external candidate is floored at
    ``known_contact`` so it is never eligible, whatever the note it replaces
    was. A trusted-pole candidate keeps its trusted stamp either way: no stamp
    can record non-admission of the user's own words without falsifying them.
    """
    external = is_external_candidate(resolved.gate_state.max_tier)
    if not resolved.ambient:
        return note_stamp_from_context(exec_context)
    if external and decision.outcome is AdmissionOutcome.ADMITTED:
        return NoteProvenanceStamp.admitted(
            title=resolved.candidate.title,
            decided_by=decision.decided_by or "the admission gate",
        )
    if external:
        return NoteProvenanceStamp.machine(
            resolved.gate_state, floor=SourceTrustTier.KNOWN_CONTACT
        )
    return NoteProvenanceStamp.machine(resolved.gate_state)


def _admission_note(
    resolved: _ResolvedWrite, decision: AmbientAdmissionDecision
) -> str | None:
    if not resolved.ambient or decision.outcome is AdmissionOutcome.NOT_GATED:
        return None
    kind = "skill" if resolved.candidate.is_skill else "note"
    if decision.outcome is AdmissionOutcome.ADMITTED:
        return f"It was reviewed and the {kind} will be loaded into context."
    return (
        f"It was saved as reference material only: the {kind} will not be "
        "loaded into context automatically, though its title stays listed and "
        f"get_note can read it. Reason: {decision.reason}"
    )


async def write_note_through_admission(
    exec_context: ToolExecutionContext,
    *,
    tool_name: str,
    title: str,
    content: str,
    include_in_prompt: bool,
    append: bool = False,
    attachment_ids: list[str] | None = None,
    visibility_labels: list[str] | None = None,
    imported_from: str | None = None,
) -> NoteWriteOutcome:
    """Write a note a model composed, reviewing it first if it will be ambient.

    Resolve the complete candidate, await the review, then persist the candidate
    and its final stamp together, conditional on the stored note not having
    changed in the meantime. If it did change, the write re-resolves and is
    reviewed again, once. See docs/design/ambient-note-admission-at-write-time.md.
    """
    # Local import: see add_or_update_note_tool.
    from family_assistant.storage.repositories.notes import (  # noqa: PLC0415
        NoteChangedError,
        NoteWritePolicyError,
        note_revision,
    )

    for attempt in range(2):
        resolved = await _resolve_candidate(
            exec_context,
            title=title,
            content=content,
            include_in_prompt=include_in_prompt,
            append=append,
            attachment_ids=attachment_ids,
            imported_from=imported_from,
        )
        decision = await _admission_decision(
            exec_context, resolved, tool_name=tool_name
        )
        if decision.outcome is AdmissionOutcome.REFUSED:
            kind = "skill" if resolved.candidate.is_skill else "note"
            return NoteWriteOutcome(
                error=(
                    f"The {kind} '{title}' would be loaded into every future "
                    f"prompt and was not admitted: {decision.reason} Nothing was "
                    "saved. It can be saved as a reference note instead "
                    "(include_in_prompt=false, no skill frontmatter)."
                ),
                admission=decision.outcome,
            )
        try:
            await exec_context.db_context.notes.add_or_update(
                title=title,
                content=resolved.candidate.content,
                include_in_prompt=include_in_prompt,
                attachment_ids=list(
                    attachment.attachment_id
                    for attachment in resolved.candidate.attachments
                ),
                visibility_labels=visibility_labels,
                write_policy=exec_context.note_write_policy(),
                provenance=_stamp_for(resolved, decision, exec_context),
                expected_revision=resolved.revision,
            )
        except NoteChangedError:
            if attempt == 0:
                logger.info("Note %r changed during its review; re-resolving.", title)
                continue
            return NoteWriteOutcome(
                error=(
                    f"Note '{title}' changed while it was being reviewed, twice; "
                    "nothing was saved. Read it again and retry."
                )
            )
        except MemoryWriteError as e:
            return NoteWriteOutcome(error=e.message)
        except NoteWritePolicyError as e:
            return NoteWriteOutcome(error=str(e))
        except Exception as e:
            logger.exception(f"Error adding/updating note '{title}': {e}")
            return NoteWriteOutcome(error=f"Failed to add/update note '{title}'. {e}")
        return NoteWriteOutcome(
            created=resolved.revision == note_revision(None),
            admission=decision.outcome,
            admission_note=_admission_note(resolved, decision),
        )
    raise AssertionError("unreachable: the write loop always returns")


# Tool Definitions
NOTE_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "add_or_update_note",
            "description": (
                "Add a new note or update an existing note with the given title. Use this for the user's own notes: "
                "lists, reference material, documents, anything they asked you to write down as a note — including "
                "when someone asks you to remember something and you have no memory tool. "
                "If `propose_memory_edits` is among your tools, prefer it for the household's long-term memory — "
                "standing preferences, facts about people, decisions, routines, and anything you are asked to forget — "
                "because it edits memory entry by entry and keeps each entry's evidence. "
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
                        "description": "Whether to auto-load the full note into your context on every turn. Default is false — the note is still stored, searchable, and its title is listed in the `<turn_context>` block so you can load it on demand via `get_note`. Set to true ONLY for short evergreen context (durable user preferences, household policies, persistent identity facts) that you want present every turn.",
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
                "Returns the note's title, content, whether it's auto-loaded into your context every turn, and any associated attachments.\n\n"
                "Returns: A JSON string containing a dict with the note information. "
                "If note exists, returns {'exists': true, 'title': [title], 'content': [full content], 'include_in_prompt': [boolean], 'attachment_count': [integer], 'provenance_labels': [labels]}. "
                "If note not found, returns {'exists': false, 'title': [title], 'content': null, 'include_in_prompt': null, 'attachment_count': 0}. "
                "Attachments are returned as multimodal content that vision models can see. "
                "When present, provenance_labels describe stored source provenance, not access-control visibility labels."
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
                "Can optionally filter to show only notes that are auto-loaded into your context every turn, or only those that are not.\n\n"
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
        title, read_policy=exec_context.note_read_policy()
    )
    if not note:
        # Fall back to file-based skills via NoteRegistry
        if exec_context.note_registry:
            skill = exec_context.note_registry.get_skill_by_name(
                title, exec_context.note_read_policy()
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
    if exec_context.taint_tracker is not None:
        read_taint = note_read_taint(
            provenance_metadata,
            title=note.title,
            labels=frozenset(note.visibility_labels),
            reason=f"Note '{note.title}' carries stored provenance taint.",
        )
        if read_taint is not None:
            merge_taint_state_into_tracker(exec_context.taint_tracker, read_taint)

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
        "provenance_labels": (
            provenance_metadata.get("provenance_labels", [])
            if isinstance(provenance_metadata, dict)
            else []
        ),
    }

    # Fetch attachment metadata and content
    attachments: list[ToolAttachment] = []
    if attachment_ids and attachment_registry:
        for attachment_id in attachment_ids:
            try:
                attachment = await _load_note_attachment(
                    exec_context,
                    attachment_registry,
                    db_context,
                    attachment_id,
                    user_id=exec_context.user_id,
                    note_title=title,
                )
            except Exception as e:
                logger.exception(
                    f"Error fetching attachment {attachment_id} for note '{title}': {e}"
                )
            else:
                if attachment is not None:
                    attachments.append(attachment)

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
        read_policy=exec_context.note_read_policy()
    )

    # Apply filtering if requested
    if include_in_prompt is not None:
        filtered_notes = [
            note for note in all_notes if note.include_in_prompt == include_in_prompt
        ]
    else:
        filtered_notes = all_notes

    if exec_context.taint_tracker is not None:
        for note in filtered_notes:
            read_taint = note_read_taint(
                note.provenance_metadata,
                title=note.title,
                labels=frozenset(note.visibility_labels),
                reason=f"Listed note '{note.title}' carries stored provenance.",
            )
            if read_taint is not None:
                merge_taint_state_into_tracker(exec_context.taint_tracker, read_taint)

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
        title, read_policy=exec_context.note_read_policy()
    )
    if not visible:
        return {
            "success": False,
            "message": f"Note '{title}' not found.",
        }
    try:
        deleted = await exec_context.db_context.notes.delete(title)
    except MemoryWriteError as e:
        return {"success": False, "message": e.message}
    return {
        "success": deleted,
        "message": f"Note '{title}' deleted successfully."
        if deleted
        else f"Note '{title}' not found.",
    }
