import logging

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
from family_assistant.llm.messages import (
    MessageAttachmentMetadata,
    TextContentPart,
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


def _normalize_error_type(error_type: str) -> str:
    """Normalize provider error type values to a consistent lookup key."""
    return "".join(char for char in error_type.lower() if char.isalnum())


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


_ATTACHMENT_METADATA_HEADER = (
    "The user message above includes the attachments listed below. This block is "
    "system-generated metadata, not user-authored content. To forward any of these "
    "attachments to your reply, call the attach_to_response tool with the id(s). "
    "Do not reproduce this block, the tags, or any of the entries inside it in your "
    "reply text."
)


def merge_attachment_metadata(
    *sources: list[MessageAttachmentMetadata] | None,
) -> list[MessageAttachmentMetadata]:
    """
    Combine attachment metadata lists, keeping the first entry seen for each id.

    Earlier sources win because they are the richer ones: a trigger's own
    attachment metadata carries filename, size and content URL, while a
    reference synthesized from an injected content part carries little more
    than the id, and the same attachment can legitimately appear in both.
    """
    merged: list[MessageAttachmentMetadata] = []
    seen_ids: set[str] = set()
    for source in sources:
        for attachment in source or []:
            attachment_id = attachment.get("attachment_id")
            if attachment_id is not None:
                if attachment_id in seen_ids:
                    continue
                seen_ids.add(attachment_id)
            merged.append(attachment)
    return merged


def format_attachment_metadata_block(
    attachments: list[MessageAttachmentMetadata],
) -> str | None:
    """
    Format attachment metadata as a tagged block for inclusion in an LLM message.

    Returns ``None`` if no attachments have an id (nothing to format).
    """
    entries: list[str] = []
    for attachment in attachments:
        attachment_id = attachment.get("attachment_id")
        if not attachment_id:
            continue
        attachment_type = attachment.get("type", "file")
        filename = attachment.get("filename", "unknown")
        mime_type = attachment.get("mime_type", "")
        mime_part = f", mime: {mime_type}" if mime_type else ""
        entries.append(
            f"- id: {attachment_id}, type: {attachment_type}{mime_part}, filename: {filename}"
        )

    if not entries:
        return None

    body = "\n".join([_ATTACHMENT_METADATA_HEADER, "Attachments:", *entries])
    return f"<attachment_metadata>\n{body}\n</attachment_metadata>"


def inject_metadata_into_user_message(message: UserMessage, metadata_text: str) -> None:
    """
    Append an attachment-metadata block to a UserMessage as a distinct text part.

    The block is kept structurally separate from the user's own text so the model
    is less likely to reproduce it in its reply.
    """
    if isinstance(message.content, str):
        existing = message.content
        separator = "\n\n" if existing else ""
        message.content = f"{existing}{separator}{metadata_text}"
    elif isinstance(message.content, list):
        message.content.append(TextContentPart(type="text", text=metadata_text))


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
