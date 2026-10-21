"""Fakes of the ``ChatInterface`` transport boundary that record every message sent."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_assistant.security.taint import TaintMetadata


@dataclass(frozen=True)
class SentChatMessage:
    """One ``send_message`` call, with every argument the interface received."""

    conversation_id: str
    text: str
    parse_mode: str | None = None
    reply_to_interface_id: str | None = None
    attachment_ids: list[str] | None = None
    on_behalf_of_user_id: str | None = None
    taint_metadata: TaintMetadata | None = None


class RecordingChatInterface:
    """Records outbound messages and returns a sequential interface message id."""

    def __init__(self, message_id_prefix: str = "sent-message") -> None:
        self.sent: list[SentChatMessage] = []
        self._message_id_prefix = message_id_prefix

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str:
        self.sent.append(
            SentChatMessage(
                conversation_id=conversation_id,
                text=text,
                parse_mode=parse_mode,
                reply_to_interface_id=reply_to_interface_id,
                attachment_ids=attachment_ids,
                on_behalf_of_user_id=on_behalf_of_user_id,
                taint_metadata=taint_metadata,
            )
        )
        return f"{self._message_id_prefix}-{len(self.sent)}"


ChatSendOutcome = str | Exception


class ScriptedChatInterface(RecordingChatInterface):
    """Records every send and answers each with a scripted outcome.

    An outcome is either the interface message id to return or an exception to
    raise. Scripted outcomes are used in order; once they run out every further
    send gets ``otherwise``. With ``otherwise=None`` a send past the end of the
    script is a test failure rather than a silent success.
    """

    def __init__(
        self,
        script: Iterable[ChatSendOutcome] = (),
        *,
        otherwise: ChatSendOutcome | None = "sent-message-id",
    ) -> None:
        super().__init__()
        self._script: deque[ChatSendOutcome] = deque(script)
        self._otherwise = otherwise

    @classmethod
    def failing_with(cls, error: Exception) -> ScriptedChatInterface:
        """An interface whose every send raises ``error``."""
        return cls(otherwise=error)

    @classmethod
    def exactly(cls, *script: ChatSendOutcome) -> ScriptedChatInterface:
        """An interface that expects exactly ``len(script)`` sends at most."""
        return cls(script, otherwise=None)

    @property
    def last_sent(self) -> SentChatMessage:
        assert self.sent, "no message was sent"
        return self.sent[-1]

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str:
        await super().send_message(
            conversation_id,
            text,
            parse_mode=parse_mode,
            reply_to_interface_id=reply_to_interface_id,
            attachment_ids=attachment_ids,
            on_behalf_of_user_id=on_behalf_of_user_id,
            taint_metadata=taint_metadata,
        )
        if self._script:
            outcome = self._script.popleft()
        elif self._otherwise is not None:
            outcome = self._otherwise
        else:
            raise AssertionError(
                f"unexpected send #{len(self.sent)} past the end of the script: {text!r}"
            )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
