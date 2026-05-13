from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.tools.types import (
    RequestConfirmationCallback as ToolRequestConfirmationCallback,
)

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from family_assistant.config_models import ToolsConfig
    from family_assistant.llm import LLMStreamEvent
    from family_assistant.llm.messages import MessageReasoningInfo, ToolMessage
    from family_assistant.skills.registry import NoteRegistry

logger = logging.getLogger(__name__)

# Backwards-compatible export for processing-layer imports.
RequestConfirmationCallback = ToolRequestConfirmationCallback


@dataclass(frozen=True)
class MidTurnUserInput:
    """User guidance received while an assistant turn is already running."""

    content: str
    interface_message_id: str | None = None
    user_name: str | None = None


class MidTurnInputProvider(Protocol):
    """Supplies user messages that should steer the next LLM iteration."""

    async def drain_pending_mid_turn_inputs(self) -> list[MidTurnUserInput]:
        """Return and consume pending mid-turn user inputs."""
        ...

    def should_interrupt(self) -> bool:
        """Return whether the active turn should halt before more work starts."""
        ...


class ChatInteractionStatus(Enum):
    """Outcome status for ProcessingService.handle_chat_interaction."""

    SUCCESS = "success"
    ERROR = "error"


class ContextPreparerConfig(Protocol):
    """Config surface required by ContextPreparer."""

    id: str
    description: str
    max_history_messages: int
    history_max_age_hours: float
    web_max_history_messages: int | None
    web_history_max_age_hours: float | None


class ToolExecutorConfig(Protocol):
    """Config surface required by ToolExecutor."""

    timezone: ZoneInfo
    id: str
    visibility_grants: set[str] | None
    default_note_visibility_labels: list[str] | None
    note_registry: NoteRegistry | None


class LLMStreamingLoopConfig(Protocol):
    """Config surface required by LLMStreamingLoop."""

    max_iterations: int
    context_pruning_min_turns: int
    tools_config: ToolsConfig


@dataclass
class ChatInteractionResult:
    """Result of a chat interaction from ProcessingService.handle_chat_interaction."""

    status: ChatInteractionStatus
    text_reply: str = ""
    assistant_message_internal_id: int | None = None
    reasoning_info: MessageReasoningInfo | None = None
    error_traceback: str | None = None
    attachment_ids: list[str] | None = None

    def __post_init__(self) -> None:
        """Enforce a consistent success/error contract."""
        if self.status == ChatInteractionStatus.SUCCESS:
            if self.error_traceback is not None:
                raise ValueError(
                    "ChatInteractionResult(status='success') cannot include error_traceback"
                )
            return

        if self.status != ChatInteractionStatus.ERROR:
            raise ValueError(f"Invalid status: {self.status!r}")

        if self.error_traceback is None:
            raise ValueError(
                "ChatInteractionResult(status='error') requires error_traceback"
            )
        if not self.text_reply:
            raise ValueError(
                "ChatInteractionResult(status='error') requires non-empty user-facing text_reply"
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
        text_reply: str = "",
        assistant_message_internal_id: int | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        attachment_ids: list[str] | None = None,
    ) -> ChatInteractionResult:
        """Create a successful chat interaction result."""
        return cls(
            status=ChatInteractionStatus.SUCCESS,
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
            status=ChatInteractionStatus.ERROR,
            text_reply=text_reply,
            assistant_message_internal_id=assistant_message_internal_id,
            reasoning_info=None,
            error_traceback=error_traceback,
            attachment_ids=None,
        )

    @property
    def has_error(self) -> bool:
        """Check if this result represents an error."""
        return self.status == ChatInteractionStatus.ERROR


@dataclass
class ToolExecutionResult:
    """Result of executing a single tool call."""

    stream_event: LLMStreamEvent
    llm_message: ToolMessage
    auto_attachment_ids: list[str] | None = None  # list of attachment IDs
    explicit_attachment_ids: list[str] | None = None

    def apply_attachment_updates(self, pending_attachment_ids: list[str]) -> None:
        """Apply attachment queue updates from this tool result to pending IDs."""
        auto_attachment_ids = self.auto_attachment_ids or []
        for attachment_id in auto_attachment_ids:
            if attachment_id not in pending_attachment_ids:
                pending_attachment_ids.append(attachment_id)
                logger.info("Auto-queued tool attachment %s for display", attachment_id)

        explicit_attachment_ids = self.explicit_attachment_ids or []
        if not explicit_attachment_ids:
            return

        old_count = len(pending_attachment_ids)
        pending_attachment_ids.clear()
        pending_attachment_ids.extend(explicit_attachment_ids)
        logger.info(
            "LLM explicitly controlled attachments: replaced %d queued with %d selected attachments",
            old_count,
            len(explicit_attachment_ids),
        )


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
    allowed_delegation_sources: list[str] | None = None
    description: str = ""  # Human-readable description of this profile
    model_parameters: dict[str, dict[str, object]] | None = (
        None  # regex pattern -> provider params mapping
    )
    fallback_model_id: str | None = None  # Added for LLM fallback
    fallback_model_parameters: dict[str, dict[str, object]] | None = (
        None  # regex pattern -> provider params mapping
    )
    # Web-specific history settings
    web_max_history_messages: int | None = None  # If None, uses max_history_messages
    web_history_max_age_hours: float | None = None  # Can be fractional
    max_iterations: int = 5
    context_pruning_min_turns: int = 3
    # Visibility grants for note access control
    visibility_grants: set[str] | None = None
    default_note_visibility_labels: list[str] | None = None
    note_registry: NoteRegistry | None = None
    greeting_wav_path: str | None = None

    def __post_init__(self) -> None:
        """Validate runtime invariants for processing config."""
        if not isinstance(self.delegation_security_level, DelegationSecurityLevel):
            raise TypeError(
                "delegation_security_level must be DelegationSecurityLevel "
                f"(got {type(self.delegation_security_level).__name__})"
            )


@dataclass
class RemoteServiceConfig:
    """Minimal config for a remote A2A profile.

    Mirrors the surface area of ProcessingServiceConfig that
    delegate_to_service and the registry depend on.
    """

    id: str
    description: str
    delegation_security_level: DelegationSecurityLevel
    allowed_delegation_sources: list[str] | None = None
    confirmation_timeout_seconds: float = 3600.0
