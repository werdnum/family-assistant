import logging
from collections.abc import Sequence

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.base import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    LLMProviderError,
    ModelNotFoundError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    MessageAttachmentMetadata,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)

logger = logging.getLogger(__name__)

_ERROR_TYPE_TO_EXCEPTION: dict[str, type[LLMProviderError]] = {
    "RateLimitError": RateLimitError,
    "ContextLengthError": ContextLengthError,
    "AuthenticationError": AuthenticationError,
    "InvalidRequestError": InvalidRequestError,
    "ModelNotFoundError": ModelNotFoundError,
    "ProviderConnectionError": ProviderConnectionError,
    "ProviderTimeoutError": ProviderTimeoutError,
    "ServiceUnavailableError": ServiceUnavailableError,
}

_NORMALIZED_ERROR_TYPE_TO_EXCEPTION: dict[str, type[LLMProviderError]] = {
    # Canonical class names
    "ratelimiterror": RateLimitError,
    "contextlengtherror": ContextLengthError,
    "authenticationerror": AuthenticationError,
    "invalidrequesterror": InvalidRequestError,
    "modelnotfounderror": ModelNotFoundError,
    "providerconnectionerror": ProviderConnectionError,
    "providertimeouterror": ProviderTimeoutError,
    "serviceunavailableerror": ServiceUnavailableError,
    # Provider snake_case / shorthand variants
    "ratelimit": RateLimitError,
    "contextlength": ContextLengthError,
    "authentication": AuthenticationError,
    "invalidrequest": InvalidRequestError,
    "modelnotfound": ModelNotFoundError,
    "connection": ProviderConnectionError,
    "timeout": ProviderTimeoutError,
    "serviceunavailable": ServiceUnavailableError,
}


def assistant_message_has_thought_signature(message: AssistantMessage) -> bool:
    """Return True when an assistant tool call carries a Google thought signature."""
    if not message.tool_calls:
        return False

    for tool_call in message.tool_calls:
        provider_metadata = tool_call.provider_metadata
        if isinstance(provider_metadata, GeminiProviderMetadata):
            if provider_metadata.thought_signature:
                return True
            continue
        if (
            isinstance(provider_metadata, dict)
            and provider_metadata.get("provider") == "google"
            and "thought_signature" in provider_metadata
        ):
            return True

    return False


def _normalize_error_type(error_type: str) -> str:
    """Normalize provider error type values to a consistent lookup key."""
    return "".join(char for char in error_type.lower() if char.isalnum())


def prune_messages_for_context(
    messages: Sequence[LLMMessage],
    *,
    min_turns: int = 3,
) -> list[LLMMessage]:
    """Prune messages to reduce context length.

    Strategy:
    1. Replace old ToolMessage content with compact placeholders (preserving the
       latest turn's tool results intact).
    2. Drop the oldest non-system turns when there are more than ``min_turns``
       user/assistant turns, keeping only the most recent ``min_turns`` turns
       and all system messages.

    Returns a new list; the input list is not modified.
    """
    # --- Step 1: identify tool_call_ids from the latest assistant tool-call block ---
    # Find the most recent AssistantMessage that issued tool calls,
    # even if the message list ends with a UserMessage.
    last_turn_tool_ids: set[str] = set()
    for msg in reversed(messages):
        if isinstance(msg, AssistantMessage) and msg.tool_calls:
            last_turn_tool_ids.update(tc.id for tc in msg.tool_calls)
            # We've found the latest assistant tool-call block; stop scanning.
            break

    # Replace old tool results with compact placeholders
    pruned: list[LLMMessage] = []
    total_before = 0
    total_after = 0
    for msg in messages:
        if isinstance(msg, ToolMessage):
            original_size = len(msg.content)
            placeholder = f"[Tool result truncated — originally {original_size} chars]"

            if (
                msg.tool_call_id not in last_turn_tool_ids
                and original_size > len(placeholder)
                and not msg.content.startswith("[Tool result truncated")
            ):
                total_before += original_size
                total_after += len(placeholder)
                pruned.append(msg.model_copy(update={"content": placeholder}))
            else:
                total_before += original_size
                total_after += original_size
                pruned.append(msg)
        else:
            pruned.append(msg)

    if total_before > 0 and (total_before - total_after) > total_before * 0.3:
        logger.info(
            "Context pruning: truncated old tool results "
            f"({total_before} -> {total_after} chars)"
        )

    # --- Step 2: drop oldest non-system turns ---
    # Identify turn boundaries: a "turn" starts with a UserMessage
    system_messages: list[LLMMessage] = []
    turns: list[list[LLMMessage]] = []
    current_turn: list[LLMMessage] = []

    for msg in pruned:
        if isinstance(msg, SystemMessage):
            system_messages.append(msg)
            continue
        if isinstance(msg, UserMessage) and current_turn:
            turns.append(current_turn)
            current_turn = []
        current_turn.append(msg)
    if current_turn:
        turns.append(current_turn)

    if min_turns < 1:
        raise ValueError("min_turns must be >= 1")

    if len(turns) > min_turns:
        kept_turns = turns[-min_turns:]
        dropped = len(turns) - min_turns
        logger.info(
            f"Context pruning: dropped {dropped} oldest turns, "
            f"keeping {min_turns} most recent"
        )
        result: list[LLMMessage] = list(system_messages)
        for turn in kept_turns:
            result.extend(turn)
        return result

    return pruned


def _user_friendly_error_message(exc: Exception) -> str:
    """Return a user-friendly error message based on the exception type."""
    if isinstance(exc, RateLimitError):
        if exc.retry_after is not None:
            return (
                f"I'm temporarily rate-limited by my AI provider. "
                f"Please try again in about {int(exc.retry_after)} seconds."
            )
        return (
            "I'm temporarily rate-limited by my AI provider. "
            "Please try again in a minute or two."
        )
    if isinstance(exc, ContextLengthError):
        return (
            "Our conversation has grown too long for me to process. "
            "Please start a new conversation."
        )
    return (
        "I encountered an error while processing your message. "
        "Please try again, and if the problem persists, contact support."
    )


def _map_stream_error_to_exception(event: LLMStreamEvent) -> Exception:
    """Map an LLM stream error event to a typed exception.

    When the streaming provider yields an error event (because content was already
    emitted and it can't raise), this converts the metadata back to a typed exception
    so the caller can handle it appropriately.
    """
    error_msg = event.error or "Unknown streaming error"
    metadata = event.metadata or {}
    error_type = metadata.get("error_type", "")
    provider = metadata.get("provider", "unknown")
    model = metadata.get("model", "unknown")

    exc_class = _ERROR_TYPE_TO_EXCEPTION.get(error_type)
    if exc_class is None and error_type:
        normalized_error_type = _normalize_error_type(error_type)
        exc_class = _NORMALIZED_ERROR_TYPE_TO_EXCEPTION.get(normalized_error_type)
    if exc_class:
        return exc_class(error_msg, provider=provider, model=model)

    return RuntimeError(f"LLM streaming error: {error_msg}")


def generate_attachment_metadata_lines(
    attachments: list[MessageAttachmentMetadata],
) -> list[str]:
    """
    Generate attachment metadata lines for a list of attachments.

    Args:
        attachments: List of attachment dictionaries

    Returns:
        List of formatted attachment metadata strings
    """
    attachment_metadata_lines = []

    for attachment in attachments:
        attachment_id = attachment.get("attachment_id")
        attachment_type = attachment.get("type", "file")
        filename = attachment.get("filename", "unknown")
        mime_type = attachment.get("mime_type", "")

        if attachment_id:
            # Format attachment metadata line
            type_desc = attachment_type
            if mime_type:
                type_desc = f"{attachment_type} ({mime_type})"

            attachment_metadata_lines.append(
                f"[Attachment available: {attachment_id} ({type_desc}: {filename})]"
            )

    return attachment_metadata_lines


def inject_metadata_into_user_message(message: UserMessage, metadata_text: str) -> None:
    """
    Inject attachment metadata text into a UserMessage.

    Modifies the message content in place to include the metadata.
    For string content, appends the metadata.
    For list content, appends metadata to the first text part or adds a new text part.

    Args:
        message: The UserMessage to modify
        metadata_text: The metadata text to inject
    """
    if isinstance(message.content, str):
        # Simple string content - append metadata
        message.content = message.content + "\n" + metadata_text
    elif isinstance(message.content, list):
        # Find text part index first, then modify after loop to avoid mutation during iteration
        text_part_idx: int | None = None
        for i, part in enumerate(message.content):
            if isinstance(part, TextContentPart) or (
                isinstance(part, dict) and part.get("type") == "text"
            ):
                text_part_idx = i
                break

        if text_part_idx is not None:
            # Found a text part - modify it
            part = message.content[text_part_idx]
            if isinstance(part, TextContentPart):
                # Replace immutable TextContentPart with new one
                message.content[text_part_idx] = TextContentPart(
                    type="text", text=part.text + "\n" + metadata_text
                )
            else:
                # Dynamic dict access in union type (part can be dict or TextContentPart)
                part["text"] = part["text"] + "\n" + metadata_text  # type: ignore[typeddict-item] # Union type contains dict subtype
        else:
            # No text part - add one at the beginning
            message.content.insert(  # type: ignore[arg-type] # TextContentPart is valid element in union list
                0, TextContentPart(type="text", text=metadata_text)
            )


def get_file_extension_from_mime_type(mime_type: str) -> str:
    """
    Return the appropriate file extension (with leading dot) for a given MIME type.

    If the MIME type has an exact match in the internal mapping, the corresponding extension is returned.
    If there is no exact match, a generic extension is returned based on the main type (e.g., '.img' for images, '.audio' for audio).
    If the main type is unrecognized, '.bin' is returned as a default.

    All returned extensions include a leading dot (e.g., '.jpg', '.bin').
    """
    # Common MIME type to extension mappings
    mime_to_ext = {
        # Images
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/svg+xml": ".svg",
        # Documents
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        # Text files
        "text/plain": ".txt",
        "text/csv": ".csv",
        "text/html": ".html",
        "application/json": ".json",
        "application/xml": ".xml",
        # Audio
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/ogg": ".ogg",
        # Video
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/ogg": ".ogv",
    }

    # Try exact match first
    if mime_type in mime_to_ext:
        return mime_to_ext[mime_type]

    # Fallback to generic extension based on main type
    main_type = (
        mime_type.split("/", maxsplit=1)[0] if "/" in mime_type else "application"
    )
    fallback_extensions = {
        "image": ".img",
        "audio": ".audio",
        "video": ".video",
        "text": ".txt",
        "application": ".bin",
    }

    return fallback_extensions.get(main_type, ".bin")
