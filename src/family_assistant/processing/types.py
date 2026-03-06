from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from family_assistant.llm import LLMStreamEvent
    from family_assistant.llm.messages import ToolMessage
    from family_assistant.skills.registry import NoteRegistry


@dataclass
class ChatInteractionResult:
    """Result of a chat interaction from ProcessingService.handle_chat_interaction."""

    text_reply: str | None = None
    assistant_message_internal_id: int | None = None
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    reasoning_info: dict[str, Any] | None = None
    error_traceback: str | None = None
    attachment_ids: list[str] | None = None

    @property
    def has_error(self) -> bool:
        """Check if this result represents an error."""
        return self.error_traceback is not None


@dataclass
class ToolExecutionResult:
    """Result of executing a single tool call."""

    stream_event: LLMStreamEvent
    llm_message: ToolMessage
    auto_attachment_ids: list[str] | None = None  # list of attachment IDs


@dataclass
class ProcessingServiceConfig:
    """Configuration specific to a ProcessingService instance."""

    prompts: dict[str, str]
    timezone: ZoneInfo
    max_history_messages: int
    history_max_age_hours: float  # Can be fractional (e.g., 0.5 hours)
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    tools_config: dict[
        str, Any
    ]  # Added to hold tool configurations like 'confirm_tools'
    delegation_security_level: str  # "blocked", "confirm", "unrestricted"
    id: str  # Unique identifier for this service profile
    description: str = ""  # Human-readable description of this profile
    # Type hint for model_parameters should reflect pattern -> params_dict structure
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    model_parameters: dict[str, dict[str, Any]] | None = None  # Corrected type
    fallback_model_id: str | None = None  # Added for LLM fallback
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
