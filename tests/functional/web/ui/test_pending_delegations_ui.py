"""The chat shows background delegations from handoff until their result lands."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import UserMessage
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage

if TYPE_CHECKING:
    from family_assistant.web.conversation_stream_hub import ConversationStreamHub


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_chip_appears_on_handoff_and_clears_when_the_result_is_delivered(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conversation_id = f"web_conv_{uuid.uuid4()}"
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Research hiking trails near Hobart"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
    )
    delegation_id = f"deleg-{uuid.uuid4().hex[:8]}"
    await db.delegation_runs.create_run(
        DelegationRunCreate(
            delegation_id=delegation_id,
            task_id=f"task-{delegation_id}",
            source_profile_id="default_assistant",
            target_service_id="research",
            interface_type="web",
            conversation_id=conversation_id,
            subconversation_id=f"sub-{delegation_id}",
            request_text="Find the best hiking trails near Hobart.",
            content_parts_json=[],
            user_id="test_user",
        )
    )
    assert await db.delegation_runs.mark_handed_off(delegation_id, datetime.now(UTC))

    page = web_test_fixture.page
    chat = ChatPage(page, web_test_fixture.base_url)
    # Wait for the follow stream so the delivery tickle below reaches this tab.
    async with page.expect_request(
        lambda request: f"/conversations/{conversation_id}/stream" in request.url,
        timeout=20000,
    ):
        await chat.navigate_to_chat(conversation_id)

    chip = page.get_by_test_id("pending-delegation")
    await chip.wait_for(state="visible", timeout=10000)
    assert "Research" in await chip.inner_text()
    assert (
        await chip.get_attribute("title") == "Find the best hiking trails near Hobart."
    )

    now = datetime.now(UTC)
    await db.delegation_runs.mark_completed(
        delegation_id=delegation_id,
        result_text="Three good trails.",
        result_attachment_ids=[],
        completed_at=now,
    )
    await db.delegation_runs.mark_notified(
        delegation_id=delegation_id,
        result_message_internal_id=None,
        notified_at=now,
    )
    app = web_test_fixture.assistant.fastapi_app
    assert app is not None
    hub: ConversationStreamHub = app.state.conversation_stream_hub
    await hub.publish(
        conversation_id,
        "message",
        turn_id=None,
        payload={"conversation_id": conversation_id, "new_messages": True},
    )

    await page.get_by_test_id("pending-delegations").wait_for(
        state="detached", timeout=10000
    )
