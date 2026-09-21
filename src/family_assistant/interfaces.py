"""
Defines abstract interfaces for communication channels.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from family_assistant.security.taint import TaintMetadata


class ChatDeliveryError(RuntimeError):
    """A chat interface could not deliver a message.

    Carries whether the identical send could succeed later, which is the
    interface's judgement to make: only the transport knows that a Telegram
    ``BadRequest`` will be refused again forever while a ``TimedOut`` may not
    be. Callers use it to choose between retrying the same text and doing
    something else with it.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class ChatInterface(Protocol):
    """
    Protocol defining how the assistant sends messages back to a chat interface.
    """

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,  # e.g., "MarkdownV2", "HTML", "PlainText"
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str]
        | None = None,  # Attachment IDs to send with the message
        on_behalf_of_user_id: str | None = None,
        taint_metadata: "TaintMetadata | None" = None,
        # Potentially add other common parameters like inline keyboard markup
        # For now, keeping it simple with text, parse_mode, reply_to, and attachments.
    ) -> str:  # Returns the interface-specific message ID
        """
        Sends a message to the specified conversation.

        Args:
            conversation_id: The unique identifier for the conversation/chat.
            text: The message text to send.
            parse_mode: Optional string indicating the formatting mode (e.g., "MarkdownV2").
            reply_to_interface_id: Optional interface-specific ID of the message to reply to.
            attachment_ids: Optional list of attachment IDs to send with the message.
            on_behalf_of_user_id: Canonical id of the user the delivery acts as,
                used as the acting user when reading owner-scoped attachments.
                ``None`` reads ownerless attachments only.
            taint_metadata: Runtime taint state of the turn that produced the
                message. Interfaces that persist the delivery to message history
                (e.g. web) record it on the stored row; transport-only
                interfaces ignore it.

        Returns:
            The interface-specific ID of the sent message.

        Raises:
            ChatDeliveryError: The message was not delivered. ``transient`` says
                whether sending the same text again could succeed.
        """
        ...

    # Add other methods if tools/tasks need other interactions.
