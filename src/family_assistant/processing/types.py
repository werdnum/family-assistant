from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from family_assistant.config_models import ToolsConfig
    from family_assistant.llm import LLMStreamEvent
    from family_assistant.llm.messages import MessageReasoningInfo, ToolMessage
    from family_assistant.skills.registry import NoteRegistry
    from family_assistant.tools import ToolExecutionContext

DelegationSecurityLevel = Literal["blocked", "confirm", "unrestricted"]

# ast-grep-ignore: no-dict-any - tool args have varying keys per tool
RequestConfirmationCallback = Callable[
    [
        str,
        str,
        str | None,
        str,
        str,
        dict[str, Any],
        float,
        "ToolExecutionContext",
    ],
    Awaitable[bool],
]


@dataclass
class ChatInteractionResult:
    """Result of a chat interaction from ProcessingService.handle_chat_interaction."""

    status: Literal["success", "error"]
    text_reply: str | None = None
    assistant_message_internal_id: int | None = None
    reasoning_info: MessageReasoningInfo | None = None
    error_traceback: str | None = None
    attachment_ids: list[str] | None = None

    def __post_init__(self) -> None:
        """Enforce a consistent success/error contract."""
        if self.status == "success":
            if self.error_traceback is not None:
                raise ValueError(
                    "ChatInteractionResult(status='success') cannot include error_traceback"
                )
            return

        if self.error_traceback is None:
            raise ValueError(
                "ChatInteractionResult(status='error') requires error_traceback"
            )
        if self.text_reply is None:
            raise ValueError(
                "ChatInteractionResult(status='error') requires user-facing text_reply"
            )
        if self.reasoning_info is not None:
            raise ValueError(
                "ChatInteractionResult(status='error') cannot include reasoning_info"
            )
        if self.attachment_ids is not None:
            raise ValueError(
                "ChatInteractionResult(status='error') cannot include attachment_ids"
            )

    @classmethod
    def success(
        cls,
        *,
        text_reply: str | None = None,
        assistant_message_internal_id: int | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        attachment_ids: list[str] | None = None,
    ) -> ChatInteractionResult:
        """Create a successful chat interaction result."""
        return cls(
            status="success",
            text_reply=text_reply,
            assistant_message_internal_id=assistant_message_internal_id,
            reasoning_info=reasoning_info,
            error_traceback=None,
            attachment_ids=attachment_ids,
        )

    @classmethod
    def error(
        cls,
        *,
        text_reply: str,
        error_traceback: str,
        assistant_message_internal_id: int | None = None,
    ) -> ChatInteractionResult:
        """Create an error chat interaction result."""
        return cls(
            status="error",
            text_reply=text_reply,
            assistant_message_internal_id=assistant_message_internal_id,
            reasoning_info=None,
            error_traceback=error_traceback,
            attachment_ids=None,
        )

    @property
    def has_error(self) -> bool:
        """Check if this result represents an error."""
        return self.status == "error"


@dataclass
class ToolExecutionResult:
    """Result of executing a single tool call."""

    stream_event: LLMStreamEvent
    llm_message: ToolMessage
    auto_attachment_ids: list[str] | None = None  # list of attachment IDs
    explicit_attachment_ids: list[str] | None = None


@dataclass
class ProcessingServiceConfig:
    """Configuration specific to a ProcessingService instance."""

    prompts: dict[str, str]
    timezone: ZoneInfo
    max_history_messages: int
    history_max_age_hours: float  # Can be fractional (e.g., 0.5 hours)
    tools_config: ToolsConfig
    delegation_security_level: DelegationSecurityLevel
    id: str  # Unique identifier for this service profile
    description: str = ""  # Human-readable description of this profile
    # Type hint for model_parameters should reflect pattern -> params_dict structure
    # ast-grep-ignore: no-dict-any - maps regex patterns to provider-specific parameter dicts
    model_parameters: dict[str, dict[str, Any]] | None = None  # Corrected type
    fallback_model_id: str | None = None  # Added for LLM fallback
    # ast-grep-ignore: no-dict-any - maps regex patterns to provider-specific parameter dicts
    fallback_model_parameters: dict[str, dict[str, Any]] | None = None  # Corrected type
    # Web-specific history settings
    web_max_history_messages: int | None = None  # If None, uses max_history_messages
    web_history_max_age_hours: float | None = None  # Can be fractional
    max_iterations: int = 5
    # Visibility grants for note access control
    visibility_grants: set[str] | None = None
    default_note_visibility_labels: list[str] | None = None
    note_registry: NoteRegistry | None = None
    greeting_wav_path: str | None = None
