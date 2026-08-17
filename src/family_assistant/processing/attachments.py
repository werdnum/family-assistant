import base64
import json
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, timedelta

import aiofiles
from pydantic import TypeAdapter

from family_assistant.config_models import AppConfig
from family_assistant.llm import LLMInterface
from family_assistant.llm.messages import (
    ContentPart,
    ContentPartDict,
    ImageUrlContentPart,
    LLMMessage,
    MessageAttachmentMetadata,
    UserMessage,
)
from family_assistant.security.taint import (
    TurnTaintState,
    artifact_taint_sources,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolAttachment, ToolDefinition
from family_assistant.utils.clock import Clock

from .utils import get_file_extension_from_mime_type

logger = logging.getLogger(__name__)


class AttachmentSelectionError(RuntimeError):
    """Raised when attachment selection cannot be completed correctly."""


@dataclass(frozen=True)
class ProcessedContentParts:
    """Injection messages built from content parts, and the attachments they carry.

    The attachment list is what lets the caller name these attachments to the
    model. Injection puts the bytes in front of it; without the ids alongside,
    a profile handed an image can look at it but cannot pass it to any tool.
    """

    messages: list[LLMMessage]
    attachments: list[MessageAttachmentMetadata]


class AttachmentProcessor:
    """Handles attachment processing for the LLM interaction pipeline."""

    def __init__(
        self,
        attachment_registry: AttachmentRegistry | None,
        llm_client: LLMInterface,
        app_config: AppConfig,
        clock: Clock,
    ) -> None:
        """
        Initialize the AttachmentProcessor.

        Args:
            attachment_registry: Registry for managing attachments (can be None if disabled).
            llm_client: LLM client for generating responses.
            app_config: Application configuration.
            clock: Clock instance for time operations.
        """
        self.attachment_registry = attachment_registry
        self.llm_client = llm_client
        self.app_config = app_config
        self.clock = clock

    async def process_content_parts(
        self,
        db_context: Database,
        conversation_id: str,
        content_parts: list[ContentPartDict],
        *,
        acting_user_id: str | None,
    ) -> ProcessedContentParts:
        """
        Process attachment content parts by fetching and injecting them as user messages.

        This handles {"type": "attachment", "attachment_id": "..."} content parts,
        which are created when attachments are passed through delegate_to_service.
        It converts them into proper LLM-visible attachment injections.

        Args:
            db_context: Database context for attachment queries
            conversation_id: Current conversation ID for security validation
            content_parts: List of content parts that may contain attachment references
            acting_user_id: Acting user for owner-scoped attachment access.

        Returns:
            The injection messages, and metadata for every attachment they carry.
        """
        injection_messages: list[LLMMessage] = []
        injected_attachments: list[MessageAttachmentMetadata] = []

        for part in content_parts:
            if part.get("type") == "attachment":
                if not self.attachment_registry:
                    raise RuntimeError(
                        "Received attachment content part but no attachment_registry is configured"
                    )
                attachment_id = part.get("attachment_id")
                if not attachment_id:
                    raise ValueError(
                        "Attachment content part is missing required 'attachment_id'"
                    )

                attachment_metadata = await self.attachment_registry.get_attachment(
                    db_context, attachment_id, acting_user_id=acting_user_id
                )
                if not attachment_metadata:
                    raise ValueError(
                        f"Attachment '{attachment_id}' was not found in attachment registry"
                    )

                content = await self.attachment_registry.get_attachment_content(
                    db_context, attachment_id, acting_user_id=acting_user_id
                )
                if content is None:
                    raise RuntimeError(
                        f"Attachment '{attachment_id}' content could not be retrieved"
                    )

                tool_attachment = ToolAttachment(
                    content=content,
                    mime_type=attachment_metadata.mime_type,
                    attachment_id=attachment_id,
                    description=attachment_metadata.description or "Attachment",
                )
                injection_msg = self.llm_client.create_attachment_injection(
                    tool_attachment
                )
                # The injected message *is* the attachment's content as the
                # model sees it, so it has to carry the attachment's taint.
                # Without this the turn reads as trusted no matter what the
                # file's provenance says -- and a text/CSV/JSON attachment is
                # injected as text, so its content reaches the model directly.
                attachment_sources = artifact_taint_sources(
                    attachment_metadata.metadata,
                    source_id=attachment_id,
                    reason="Attachment injected into the turn.",
                )
                if attachment_sources and injection_msg.taint_metadata is None:
                    attachment_state = TurnTaintState.empty()
                    for source in attachment_sources:
                        attachment_state = attachment_state.add_source(source)
                    injection_msg.taint_metadata = attachment_state.to_metadata()
                injection_messages.append(injection_msg)
                injected_attachments.append(
                    MessageAttachmentMetadata(
                        type="attachment_reference",
                        attachment_id=attachment_id,
                        mime_type=attachment_metadata.mime_type,
                        description=attachment_metadata.description,
                        filename=attachment_metadata.metadata.get(
                            "original_filename", "attachment"
                        ),
                        size=attachment_metadata.size,
                    )
                )
                logger.info(
                    "Processed attachment content part %s for LLM injection",
                    attachment_id,
                )
            elif part.get("type") == "image_url":
                # image_url parts (e.g. from A2A FileParts) go directly to the LLM
                # as injection messages — the LLM handles URLs and data URIs natively
                url = part.get("image_url", {}).get("url", "")
                if url:
                    injection_messages.append(
                        UserMessage(
                            content=[
                                ImageUrlContentPart(
                                    type="image_url", image_url={"url": url}
                                )
                            ]
                        )
                    )
                    image_attachment_id = part.get("attachment_id")
                    if image_attachment_id:
                        injected_attachments.append(
                            MessageAttachmentMetadata(
                                type="attachment_reference",
                                attachment_id=image_attachment_id,
                            )
                        )
        return ProcessedContentParts(
            messages=injection_messages, attachments=injected_attachments
        )

    async def convert_urls_to_data_uris(
        self,
        db_context: Database,
        content_parts: list[ContentPartDict],
        *,
        acting_user_id: str | None,
    ) -> list[ContentPartDict]:
        """
        Convert any attachment server URLs in content parts to data URIs.

        This is necessary because external LLM providers cannot access our internal
        server URLs like /api/attachments/...

        Args:
            db_context: The turn's database context — attachments registered
                earlier in this transaction must be visible here.
            content_parts: List of content parts that may contain image_url entries
            acting_user_id: Acting user for owner-scoped attachment resolution.

        Returns:
            Modified content parts with server URLs converted to data URIs
        """
        # If no attachment service is available, return parts unchanged
        if not self.attachment_registry:
            return content_parts

        converted_parts = []

        for part in content_parts:
            if part.get("type") == "image_url":
                image_url = part.get("image_url", {}).get("url", "")

                # Check if it's a server URL that needs conversion
                if image_url.startswith("/api/attachments/"):
                    # Extract attachment ID from URL
                    match = re.match(r"/api/attachments/([a-f0-9-]+)", image_url)
                    if not match:
                        raise ValueError(
                            f"Invalid attachment URL format, cannot extract attachment ID: {image_url}"
                        )

                    attachment_id = match.group(1)

                    # Use AttachmentRegistry to resolve the file path. The
                    # async variant looks up ``attachment_metadata.storage_path``
                    # internally so externally-managed files (e.g. email
                    # attachments in the mailbox directory) resolve too.
                    file_path = await self.attachment_registry.resolve_attachment_path(
                        attachment_id, db_context, acting_user_id=acting_user_id
                    )
                    if not file_path or not file_path.exists():
                        raise FileNotFoundError(
                            f"Attachment file not found for ID: {attachment_id}"
                        )

                    # Read file asynchronously
                    async with aiofiles.open(file_path, "rb") as f:
                        file_bytes = await f.read()

                    # Detect MIME type from file extension
                    content_type = self.attachment_registry.get_content_type(file_path)

                    # Convert to base64
                    base64_data = base64.b64encode(file_bytes).decode("utf-8")
                    data_uri = f"data:{content_type};base64,{base64_data}"

                    # Replace with a data URI, keeping the attachment's identity.
                    # Inlining the bytes is what lets a provider read the file;
                    # carrying the id alongside is what lets one that cannot read
                    # it still name the file and hand it to a model that can.
                    converted_parts.append({
                        "type": "image_url",
                        "image_url": {"url": data_uri},
                        "attachment_id": attachment_id,
                    })
                    logger.info(
                        "Converted attachment URL to data URI for attachment %s (type: %s)",
                        attachment_id,
                        content_type,
                    )
                else:
                    # Already a data URI or external URL, keep as-is
                    converted_parts.append(part)
            else:
                # Not an image_url part, keep as-is
                converted_parts.append(part)

        return converted_parts

    async def convert_message_urls(
        self,
        db_context: Database,
        messages: list[LLMMessage],
        *,
        acting_user_id: str | None,
    ) -> list[LLMMessage]:
        """
        Convert any attachment server URLs in message content to data URIs.

        This applies the URL-to-data-URI conversion to all user messages in the list.

        Args:
            db_context: The turn's database context.
            messages: List of LLM messages
            acting_user_id: Acting user for owner-scoped attachment resolution.

        Returns:
            Modified messages with server URLs converted to data URIs
        """
        if not self.attachment_registry:
            return messages

        # TypeAdapter for deserializing ContentPart union from dicts
        content_part_adapter: TypeAdapter[ContentPart] = TypeAdapter(ContentPart)

        converted_messages: list[LLMMessage] = []

        for msg in messages:
            if isinstance(msg, UserMessage) and isinstance(msg.content, list):
                # Serialize content parts to dicts using Pydantic's model_dump
                content_dicts: list[ContentPartDict] = [
                    part.model_dump()
                    for part in msg.content  # type: ignore[misc]  # msg.content is list[ContentPart] which all have model_dump()
                ]

                # Apply URL conversion
                converted_dicts = await self.convert_urls_to_data_uris(
                    db_context, content_dicts, acting_user_id=acting_user_id
                )

                # Deserialize back to ContentPart objects using TypeAdapter
                converted_parts: list[ContentPart] = [
                    content_part_adapter.validate_python(part_dict)
                    for part_dict in converted_dicts
                ]

                # model_copy rather than a fresh UserMessage: constructing one
                # keeps only the content and silently drops every other field --
                # taint_metadata, and is_turn_scaffolding, which four separate
                # scans rely on to tell machinery from what the user said.
                converted_messages.append(
                    msg.model_copy(update={"content": converted_parts})
                )
            else:
                # Keep non-user messages and string-content messages as-is
                converted_messages.append(msg)

        return converted_messages

    async def extract_conversation_context(
        self,
        db_context: Database,
        conversation_id: str,
        max_age_hours: float,
        prompts: dict[str, str],
        *,
        acting_user_id: str | None,
    ) -> str:
        """
        Extracts recent attachment information from the conversation and formats it for LLM context.

        Args:
            db_context: Database context for attachment queries.
            conversation_id: Conversation identifier to query attachments for.
            max_age_hours: Maximum age of attachments to include (in hours).
            prompts: Dictionary of prompt templates.
            acting_user_id: Acting user; owned rows are surfaced only for a match.

        Returns:
            Formatted string with attachment context, or empty string if no attachments found.
        """
        if not self.attachment_registry:
            return ""

        # Query recent attachments using storage layer method
        cutoff_time = self.clock.now() - timedelta(hours=max_age_hours)
        attachments = (
            await self.attachment_registry.get_recent_attachments_for_conversation(
                db_context=db_context,
                conversation_id=conversation_id,
                max_age=cutoff_time,
                acting_user_id=acting_user_id,
            )
        )

        if not attachments:
            return ""

        # Format attachment context
        attachment_items = []
        now = self.clock.now()

        for attachment in attachments:
            attachment_id = attachment.attachment_id
            filename = attachment.description or "unknown"
            content_type = attachment.mime_type or "unknown"
            created_at = attachment.created_at

            # Ensure created_at is timezone-aware (SQLite may return naive datetimes)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)

            # Calculate age
            age = now - created_at
            if age.total_seconds() < 3600:  # Less than 1 hour
                minutes = int(age.total_seconds() / 60)
                age_str = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            else:
                hours = int(age.total_seconds() / 3600)
                age_str = f"{hours} hour{'s' if hours != 1 else ''} ago"

            attachment_items.append(
                f"- [{attachment_id}] {filename} ({content_type}) - {age_str}"
            )

        # Use prompt template
        items_str = "\n".join(attachment_items)
        header_template = prompts.get(
            "thread_attachments_context_header",
            "Recent Attachments in Conversation:\n{attachments_list}",
        )
        return header_template.format(attachments_list=items_str)

    async def select_for_response(
        self,
        pending_attachment_ids: list[str],
        original_query: str,
        *,
        acting_user_id: str | None,
    ) -> list[str]:
        """
        Select the most relevant attachments to include in the response.

        Uses LLM to intelligently select which attachments are most relevant
        to the user's original query, respecting the max_response_attachments limit.

        Args:
            pending_attachment_ids: List of available attachment IDs to choose from
            original_query: The original user query to evaluate relevance
            acting_user_id: Acting user; owned rows are surfaced only for a match.

        Returns:
            List of selected attachment IDs (up to max_response_attachments)
        """
        if not pending_attachment_ids or not self.attachment_registry:
            return pending_attachment_ids

        attachment_descriptions: list[str] = []
        for att_id in pending_attachment_ids:
            metadata = await self.attachment_registry.get_attachment_with_context(
                att_id, acting_user_id=acting_user_id
            )
            if metadata:
                attachment_descriptions.append(
                    f"- {att_id}: {metadata.description or 'Attachment'} ({metadata.mime_type})"
                )
            else:
                attachment_descriptions.append(f"- {att_id}: (metadata unavailable)")

        selection_prompt = f"""You have {len(pending_attachment_ids)} attachments from tool results that could be sent to the user.
The user's original query was: "{original_query}"

Select up to {self.app_config.max_response_attachments} attachments that best answer the user's question.
Prioritize:
- Images that directly answer the query
- Representative samples if many are similar
- Key findings or highlights

Available attachments:
{chr(10).join(attachment_descriptions)}

Call attach_to_response with your selected attachment IDs."""

        selection_tools: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "attach_to_response",
                    "description": "Select the most relevant attachments to include in the response to the user",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "attachment_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of attachment IDs to include",
                            }
                        },
                        "required": ["attachment_ids"],
                    },
                },
            }
        ]

        selection_messages: list[LLMMessage] = [
            UserMessage(content=selection_prompt),
        ]

        logger.debug(
            f"Selecting attachments from {len(pending_attachment_ids)} candidates using LLM"
        )

        try:
            response = await self.llm_client.generate_response(
                messages=selection_messages,
                tools=selection_tools,
                tool_choice="required",
            )
        except Exception as exc:
            raise AttachmentSelectionError(
                "Failed to run LLM-based attachment selection"
            ) from exc

        if not response.tool_calls:
            raise AttachmentSelectionError(
                "Attachment selection response did not contain any tool calls"
            )

        tool_call = response.tool_calls[0]
        if tool_call.function.name != "attach_to_response":
            raise AttachmentSelectionError(
                f"Attachment selection returned unexpected tool '{tool_call.function.name}'"
            )

        arguments = tool_call.function.arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise AttachmentSelectionError(
                    "Attachment selection returned invalid JSON arguments"
                ) from exc

        if not isinstance(arguments, dict):
            raise AttachmentSelectionError(
                f"Attachment selection arguments must be an object, got {type(arguments).__name__}"
            )

        selected_ids_raw = arguments.get("attachment_ids")
        if not isinstance(selected_ids_raw, list):
            raise AttachmentSelectionError(
                "Attachment selection arguments must include attachment_ids as an array"
            )

        selected_ids = [str(id_) for id_ in selected_ids_raw]
        pending_id_set = set(pending_attachment_ids)
        unknown_ids = [
            att_id for att_id in selected_ids if att_id not in pending_id_set
        ]
        if unknown_ids:
            raise AttachmentSelectionError(
                "Attachment selection returned unknown attachment IDs: "
                + ", ".join(unknown_ids)
            )

        selected_ids = selected_ids[: self.app_config.max_response_attachments]
        logger.info(
            "LLM selected %d attachments from %d candidates",
            len(selected_ids),
            len(pending_attachment_ids),
        )
        return selected_ids

    @staticmethod
    def _looks_like_json_content(content: str) -> bool:
        """Infer JSON shape from delimiters without fully parsing large payloads."""
        if not content:
            return False

        opening_index = 0
        while opening_index < len(content) and content[opening_index].isspace():
            opening_index += 1

        if opening_index >= len(content):
            return False

        closing_index = len(content) - 1
        while closing_index >= 0 and content[closing_index].isspace():
            closing_index -= 1

        if closing_index <= opening_index:
            return False

        opening, closing = content[opening_index], content[closing_index]
        return (opening, closing) in {("{", "}"), ("[", "]")}

    async def handle_large_result(
        self,
        db_context: Database,
        content: str,
        tool_name: str,
        conversation_id: str,
        call_id: str,
        taint_metadata: Mapping[str, object] | None = None,
        *,
        owner_user_id: str | None = None,
    ) -> tuple[str, str | None]:
        """
        Check if a tool result is too large and convert it to an attachment if necessary.

        Args:
            db_context: Database context for storing attachments.
            content: The tool result content.
            tool_name: Name of the tool that generated the result.
            conversation_id: Conversation ID for context.
            call_id: Tool call ID for metadata.
            taint_metadata: Optional taint metadata to stamp on the attachment.
            owner_user_id: Owner recorded on the auto-converted attachment. Set
                only for personal-data tool results so ordinary tool output
                stays ownerless (backward-compatible).

        Returns:
            Tuple of (new_content, auto_attachment_id)
        """
        # Threshold from config (default 100 KiB)
        threshold_kb = 100
        if self.app_config and self.app_config.attachment_config:
            threshold_kb = (
                self.app_config.attachment_config.large_tool_result_threshold_kb
            )

        THRESHOLD_BYTES = threshold_kb * 1024
        content_bytes = content.encode("utf-8")
        if len(content_bytes) < THRESHOLD_BYTES:
            return content, None

        # Exempt certain tools from auto-conversion - they already handle attachments
        # or the user explicitly requested the content
        EXEMPT_TOOLS = {"read_text_attachment"}
        if tool_name in EXEMPT_TOOLS:
            logger.debug(
                f"Tool '{tool_name}' is exempt from large result auto-conversion"
            )
            return content, None

        # Determine MIME type without a full JSON parse of large payloads.
        mime_type = (
            "application/json"
            if self._looks_like_json_content(content)
            else "text/plain"
        )

        if not self.attachment_registry:
            raise RuntimeError(
                "Tool result exceeded large-result threshold but attachment storage is unavailable: "
                f"tool='{tool_name}', bytes={len(content_bytes)}, threshold={THRESHOLD_BYTES}"
            )
        file_extension = get_file_extension_from_mime_type(mime_type)
        attachment_metadata: dict[str, object] = {
            "tool_call_id": call_id,
            "auto_display": False,
            "large_result_auto_convert": True,
        }
        if taint_metadata is not None:
            attachment_metadata["taint_metadata"] = taint_metadata

        registered_metadata = (
            await self.attachment_registry.store_and_register_tool_attachment(
                file_content=content_bytes,
                filename=f"large_result_{tool_name}_{uuid.uuid4()}{file_extension}",
                content_type=mime_type,
                tool_name=tool_name,
                description=f"Large output from {tool_name}",
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                metadata=attachment_metadata,
                db_context=db_context,
            )
        )

        att_id = registered_metadata.attachment_id

        hint = (
            f"\n\n[NOTE: This result was too large ({len(content_bytes)} bytes) "
            f"and has been automatically converted to an attachment with ID {att_id}.]"
        )
        if mime_type == "application/json":
            hint += (
                f"\nYou can use `jq_query(attachment_id='{att_id}', jq_program='...')` "
                f"to process it without loading it all into your context."
            )
        else:
            hint += (
                f"\nYou can use `read_text_attachment(attachment_id='{att_id}', ...)` "
                f"to read parts of it, or use `execute_script` to process it with a custom script."
                f"\nExample script for searching:\n"
                f"```python\n"
                f"content = attachment_read('{att_id}')\n"
                f"for line in content.split('\\n'):\n"
                f"    if 'search_term' in line:\n"
                f"        print(line)\n"
                f"```"
            )

        hint += (
            "\nThis attachment is working data: it is NOT shown to the user. "
            f"If they asked for the file itself, call attach_to_response(attachment_ids=['{att_id}'])."
        )

        new_content = f"Tool result from '{tool_name}' was too large and was saved as attachment {att_id}.{hint}"
        logger.info(
            f"Auto-converted large tool result ({len(content_bytes)} bytes) from "
            f"'{tool_name}' to attachment {att_id}"
        )
        return new_content, att_id
