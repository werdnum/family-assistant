"""Unit tests for how the web confirmation manager records durable requests."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
)
from family_assistant.web.web_confirmation_ui_manager import WebConfirmationUIManager

if TYPE_CHECKING:
    from family_assistant.services.confirmation_service import ConfirmationService


class _StopAfterCreate(Exception):
    pass


class _RecordingConfirmationService:
    def __init__(self) -> None:
        self.create_kwargs: dict[str, object] = {}

    async def create_request(self, **kwargs: object) -> None:
        self.create_kwargs = kwargs
        raise _StopAfterCreate


@pytest.mark.asyncio
async def test_request_records_the_originating_conversation() -> None:
    """The pending-approvals tray names, and links to, where a request came from."""
    service = _RecordingConfirmationService()
    manager = WebConfirmationUIManager(
        confirmation_service=cast("ConfirmationService", service),
        confirmation_result_waiters=ConfirmationResultWaiterRegistry(),
        stream_hub=None,
    )

    with pytest.raises(_StopAfterCreate):
        await manager.request_confirmation(
            conversation_id="web_conv_trip",
            interface_type="web",
            turn_id=None,
            prompt_text="Add the note?",
            tool_name="add_or_update_note",
            tool_args={"title": "Trip"},
            timeout=60,
            target_user_id="test_user",
            tool_call_id="call-1",
        )

    assert service.create_kwargs["origin_interface_type"] == "web"
    assert service.create_kwargs["origin_conversation_id"] == "web_conv_trip"
