"""
Defines abstract interfaces for communication channels.
"""

from typing import Protocol


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
        # Potentially add other common parameters like inline keyboard markup
        # For now, keeping it simple with text, parse_mode, and reply_to.
    ) -> str | None:  # Returns interface-specific message ID if successful, else None
        """
        Sends a message to the specified conversation.

        Args:
            conversation_id: The unique identifier for the conversation/chat.
            text: The message text to send.
            parse_mode: Optional string indicating the formatting mode (e.g., "MarkdownV2").
            reply_to_interface_id: Optional interface-specific ID of the message to reply to.

        Returns:
            The interface-specific ID of the sent message, or None if sending failed.
        """
        ...

    # Add other methods if tools/tasks need other interactions.
