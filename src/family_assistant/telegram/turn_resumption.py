"""Relaunch Telegram turns that an earlier process was running.

Registered with the ``TurnLeaseRegistry`` under :data:`TELEGRAM_RESUMER`. The
turn runs again under its original ``turn_id`` from the rows the interrupted run
persisted, holding the chat's turn slot so new messages steer it, and its reply
is delivered through the same path as any Telegram reply.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.services.turn_resumption import (
        TurnLeaseRegistry,
        TurnResumePayload,
    )
    from family_assistant.telegram.handler import TelegramUpdateHandler

TELEGRAM_RESUMER = "telegram"


class TelegramTurnResumer:
    """Resumes Telegram turns through the bot's update handler."""

    def __init__(self, handler: "TelegramUpdateHandler") -> None:
        self._handler = handler

    async def resume(
        self, payload: "TurnResumePayload", registry: "TurnLeaseRegistry"
    ) -> bool:
        return await self._handler.resume_interrupted_turn(payload, registry)

    async def deliver_pending_reply(self, payload: "TurnResumePayload") -> None:
        await self._handler.deliver_pending_reply(payload)
