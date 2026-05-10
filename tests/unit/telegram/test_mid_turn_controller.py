from unittest.mock import MagicMock

import pytest
from telegram import Update

from family_assistant.processing.types import MidTurnUserInput
from family_assistant.telegram.handler import TelegramMidTurnController
from family_assistant.telegram.types import AttachmentData


@pytest.mark.asyncio
async def test_drained_text_only_mid_turn_update_is_not_replayed() -> None:
    controller = TelegramMidTurnController()
    update = MagicMock(spec=Update)
    user_input = MidTurnUserInput(content="use this instead")

    await controller.add_update(update, None, user_input)

    assert await controller.drain_pending_mid_turn_inputs() == [user_input]
    assert await controller.pop_unconsumed_batch() == []


@pytest.mark.asyncio
async def test_drained_attachment_mid_turn_update_is_replayed_for_registration() -> (
    None
):
    controller = TelegramMidTurnController()
    update = MagicMock(spec=Update)
    attachment = AttachmentData(
        content=b"image-bytes",
        filename="photo.jpg",
        mime_type="image/jpeg",
    )
    user_input = MidTurnUserInput(content="look at this photo")

    await controller.add_update(update, [attachment], user_input)

    assert await controller.drain_pending_mid_turn_inputs() == [user_input]
    assert await controller.pop_unconsumed_batch() == [(update, [attachment])]


@pytest.mark.asyncio
async def test_undrained_mid_turn_update_is_replayed_after_turn_finishes() -> None:
    controller = TelegramMidTurnController()
    update = MagicMock(spec=Update)
    user_input = MidTurnUserInput(content="one more thing")

    await controller.add_update(update, None, user_input)

    assert await controller.pop_unconsumed_batch() == [(update, None)]
