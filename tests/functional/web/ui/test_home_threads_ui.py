"""The web chat lands on Home, shows each thread as a live card, and opens threads beside it."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from playwright.async_api import ViewportSize, expect
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.database import Database
from family_assistant.threads.echo import announce_home_change, echo_finished_turn
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.home_page import HomePage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

USER = "test_user"
THREAD_PROFILE = "test_research"
SEED_REQUEST = "Find three plumbers and get quotes for a new hot water system."
# Wide enough for the thread panel beside Home, with both sidebars open.
WIDE_VIEWPORT: ViewportSize = {"width": 1600, "height": 900}


@pytest.fixture
def web_conversation_threads_enabled() -> bool:
    return True


@dataclass(frozen=True)
class SeededThread:
    home: str
    thread: str
    delegation_id: str


async def _home_conversation_id(base_url: str) -> str:
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.get("/api/v1/chat/home")
    response.raise_for_status()
    home = response.json()["conversation_id"]
    assert isinstance(home, str)
    return home


async def _seed_thread(db: Database, home: str) -> SeededThread:
    """A Home turn that started a thread whose first run is still queued."""
    now = datetime.now(UTC)
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    thread = f"web_conv_{uuid.uuid4()}"
    delegation_id = f"thread-{uuid.uuid4().hex[:8]}"
    home_row = {
        "interface_type": "web",
        "conversation_id": home,
        "timestamp": now,
        "processing_profile_id": "default_assistant",
    }
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Can you get quotes for the hot water?"),
        user_id=USER,
        **home_row,
    )
    await db.message_history.add_message(
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id=call_id,
                    type="function",
                    function=ToolCallFunction(
                        name="start_thread",
                        arguments=json.dumps({
                            "target_service_id": THREAD_PROFILE,
                            "title": "Hot water system quotes",
                            "request": SEED_REQUEST,
                        }),
                    ),
                )
            ],
        ),
        **home_row,
    )
    await db.message_history.add_message(
        ToolMessage(
            tool_call_id=call_id, name="start_thread", content="Thread started."
        ),
        **home_row,
    )
    await db.message_history.add_message(
        AssistantMessage(content="On it. I've started a thread for the quotes."),
        **home_row,
    )

    await db.delegation_runs.create_run({
        "delegation_id": delegation_id,
        "task_id": f"task-{delegation_id}",
        "source_profile_id": "default_assistant",
        "target_service_id": THREAD_PROFILE,
        "interface_type": "web",
        "conversation_id": thread,
        "subconversation_id": None,
        "run_kind": "thread",
        "request_text": SEED_REQUEST,
        "content_parts_json": [],
        "user_id": USER,
    })
    await db.conversation_threads.create_thread(
        conversation_id=thread,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=USER,
        profile_id=THREAD_PROFILE,
        title="Hot water system quotes",
        title_taint=None,
        anchor_message_id=None,
        delegation_id=delegation_id,
        now=now,
        source_tool_call_id=call_id,
    )
    seed = await db.message_history.add_message(
        UserMessage.from_trusted_user(content=SEED_REQUEST),
        interface_type="web",
        conversation_id=thread,
        timestamp=now,
        processing_profile_id=THREAD_PROFILE,
        user_id=USER,
        is_internal=True,
    )
    await db.conversation_threads.set_seed_message(thread, seed, now)
    return SeededThread(home=home, thread=thread, delegation_id=delegation_id)


async def _open_home(fixture: WebTestFixture, home: str) -> HomePage:
    """Open the chat with no conversation named, once the activity stream is live."""
    page = fixture.page
    chat = HomePage(page, fixture.base_url)
    async with page.expect_request(
        lambda request: "/api/v1/chat/activity/stream" in request.url, timeout=20000
    ):
        await chat.navigate_to_chat()
    await chat.wait_for_home(home)
    return chat


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_home_card_follows_the_thread_from_working_to_waiting(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    seeded = await _seed_thread(db, home)

    chat = await _open_home(web_test_fixture, home)

    await chat.wait_for_message_content("On it. I've started a thread for the quotes.")
    card = chat.thread_card(seeded.thread)
    await expect(card).to_be_visible(timeout=15000)
    await chat.wait_for_card_state(seeded.thread, "working")
    await expect(card).to_contain_text("Hot water system quotes")
    await expect(
        chat.sidebar_group("working").get_by_test_id(f"thread-item-{seeded.thread}")
    ).to_be_visible()

    now = datetime.now(UTC)
    assert await db.delegation_runs.mark_completed(
        delegation_id=seeded.delegation_id,
        result_text=None,
        result_attachment_ids=[],
        completed_at=now,
    )
    await db.message_history.add_message(
        AssistantMessage(content="Gas or heat pump?"),
        interface_type="web",
        conversation_id=seeded.thread,
        timestamp=now,
        processing_profile_id=THREAD_PROFILE,
    )
    app = web_test_fixture.assistant.fastapi_app
    assert app is not None
    await app.state.conversation_stream_hub.publish_activity(
        seeded.thread, user_id=USER, reason="thread_run_ended"
    )

    await chat.wait_for_card_state(seeded.thread, "waiting")
    await expect(card).to_contain_text("Gas or heat pump?")
    await expect(
        chat.sidebar_group("waiting").get_by_test_id(f"thread-item-{seeded.thread}")
    ).to_be_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_a_card_opens_its_thread_beside_home_and_each_pane_answers_for_itself(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    mock_llm_client.rules = [
        (
            lambda args: "Heat pump, please" in str(args.get("messages", [])),
            LLMOutput(content="Asking Acme to quote a heat pump."),
        ),
        (
            lambda args: "Is the bin out?" in str(args.get("messages", [])),
            LLMOutput(content="Yes, it went out at seven."),
        ),
    ]
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    seeded = await _seed_thread(db, home)
    await web_test_fixture.page.set_viewport_size(WIDE_VIEWPORT)
    chat = await _open_home(web_test_fixture, home)
    await _finish_first_run_with_question(web_test_fixture, db, seeded)
    await chat.wait_for_card_state(seeded.thread, "waiting")

    home_pane = chat.pane(home)
    home_input = home_pane.locator(chat.CHAT_INPUT)
    await home_input.fill("Is the bin out?")
    await chat.open_thread_from_card(seeded.thread)

    panel = chat.pane(seeded.thread)
    await expect(panel).to_have_attribute("data-presentation", "side")
    await expect(home_pane).to_be_visible()
    await expect(home_input).to_have_value("Is the bin out?")
    banner = panel.locator(chat.THREAD_BANNER)
    await expect(banner.locator(chat.THREAD_ORIGIN)).to_contain_text(
        f"Assistant asked: “{SEED_REQUEST}”"
    )
    await expect(banner.locator(chat.THREAD_BREADCRUMB_HOME)).to_have_count(0)
    await expect(panel.locator(chat.CONVERSATION_HEADING)).to_have_text(
        "Hot water system quotes"
    )
    await expect(
        panel.get_by_role("combobox", name="Processing profile")
    ).to_be_disabled()

    await chat.send_in(seeded.thread, "Heat pump, please")
    await expect(panel.get_by_text("Asking Acme to quote a heat pump.")).to_be_visible(
        timeout=20000
    )
    await expect(home_input).to_have_value("Is the bin out?")
    await home_input.press("Enter")
    await expect(home_pane.get_by_text("Yes, it went out at seven.")).to_be_visible(
        timeout=20000
    )

    # Each reply is only in its own transcript (Home's card previews the
    # thread's latest message, so look at the transcripts' text, not the card).
    home_text = home_pane.locator(".markdown-text")
    panel_text = panel.locator(".markdown-text")
    await expect(home_text.filter(has_text="Asking Acme")).to_have_count(0)
    await expect(
        home_pane.locator(chat.MESSAGE_USER).filter(has_text="Heat pump, please")
    ).to_have_count(0)
    await expect(panel_text.filter(has_text="it went out at seven")).to_have_count(0)
    await expect(
        panel.locator(chat.MESSAGE_USER).filter(has_text="Is the bin out?")
    ).to_have_count(0)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_the_panel_expands_collapses_and_survives_a_reload(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    seeded = await _seed_thread(db, home)
    await web_test_fixture.page.set_viewport_size(WIDE_VIEWPORT)
    chat = await _open_home(web_test_fixture, home)
    page = web_test_fixture.page
    await chat.open_thread_from_card(seeded.thread)
    thread = chat.pane(seeded.thread)

    await chat.expand_panel()
    await expect(thread).to_have_attribute("data-presentation", "main")
    await expect(chat.pane(home)).to_have_count(0)
    await expect(thread.locator(chat.THREAD_BREADCRUMB_HOME)).to_be_visible()
    await page.wait_for_url(lambda url: "expanded=1" in url, timeout=15000)

    await page.reload()
    await expect(thread).to_have_attribute("data-presentation", "main", timeout=15000)
    await expect(chat.heading()).to_have_text("Hot water system quotes")
    await expect(chat.pane(home)).to_have_count(0)

    await chat.collapse_panel()
    await expect(thread).to_have_attribute("data-presentation", "side")
    await expect(chat.heading()).to_have_text("Home")
    await page.wait_for_url(
        lambda url: f"thread={seeded.thread}" in url and "expanded" not in url,
        timeout=15000,
    )

    await page.reload()
    await expect(thread).to_have_attribute("data-presentation", "side", timeout=15000)
    await expect(chat.pane(home).locator(chat.CONVERSATION_HEADING)).to_have_text(
        "Home"
    )
    await expect(thread.locator(chat.THREAD_BANNER)).to_be_visible()

    await chat.close_panel()
    await expect(thread).to_have_count(0)
    await chat.wait_for_home(home)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_a_narrow_window_opens_a_thread_in_place_of_home(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    seeded = await _seed_thread(db, home)
    page = web_test_fixture.page
    await page.set_viewport_size({"width": 1000, "height": 800})
    chat = await _open_home(web_test_fixture, home)

    await chat.open_thread_from_card(seeded.thread)

    thread = chat.pane(seeded.thread)
    await expect(thread).to_have_attribute("data-presentation", "main")
    await expect(chat.pane(home)).to_have_count(0)
    await expect(chat.heading()).to_have_text("Hot water system quotes")
    await expect(thread.locator(chat.THREAD_WORKING_INDICATOR)).to_be_visible()

    await chat.back_to_home(home)
    await expect(chat.thread_card(seeded.thread)).to_be_visible()
    await expect(chat.thread_banner()).to_have_count(0)


async def _finish_first_run_with_question(
    fixture: WebTestFixture, db: Database, seeded: SeededThread
) -> None:
    """The thread's first run ends on a question, leaving it waiting on the user."""
    now = datetime.now(UTC)
    assert await db.delegation_runs.mark_completed(
        delegation_id=seeded.delegation_id,
        result_text=None,
        result_attachment_ids=[],
        completed_at=now,
    )
    await _add_thread_reply(db, seeded, "Gas or heat pump?")
    await _hub(fixture).publish_activity(
        seeded.thread, user_id=USER, reason="thread_run_ended"
    )


async def _add_thread_reply(
    db: Database, seeded: SeededThread, content: str, turn_id: str | None = None
) -> None:
    await db.message_history.add_message(
        AssistantMessage(content=content),
        interface_type="web",
        conversation_id=seeded.thread,
        timestamp=datetime.now(UTC),
        processing_profile_id=THREAD_PROFILE,
        turn_id=turn_id,
    )


def _hub(fixture: WebTestFixture) -> ConversationStreamHub:
    app = fixture.assistant.fastapi_app
    assert app is not None
    return app.state.conversation_stream_hub


async def _home_threads(base_url: str) -> list[dict]:
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.get("/api/v1/chat/home/threads")
    response.raise_for_status()
    return response.json()["threads"]


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_card_follows_a_thread_to_done_with_its_echo_and_back_when_it_reopens(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    seeded = await _seed_thread(db, home)
    chat = await _open_home(web_test_fixture, home)
    await _finish_first_run_with_question(web_test_fixture, db, seeded)
    await chat.wait_for_card_state(seeded.thread, "waiting")

    # The thread's assistant finishes the job: its closing turn's reply is
    # echoed into Home, as the end of a real closing turn does.
    closing_turn = str(uuid.uuid4())
    await _add_thread_reply(
        db, seeded, "Acme is cheapest at $2,400; booked for Tuesday.", closing_turn
    )
    await db.conversation_threads.close_thread(
        seeded.thread,
        outcome="done",
        closed_by="assistant",
        turn_id=closing_turn,
        now=datetime.now(UTC),
    )
    default_service = web_test_fixture.assistant.default_processing_service
    assert default_service is not None
    echo = await echo_finished_turn(
        db,
        conversation_id=seeded.thread,
        turn_id=closing_turn,
        home_profile=default_service.service_config,
    )
    assert echo is not None
    await announce_home_change(
        _hub(web_test_fixture),
        home_conversation_id=home,
        user_id=USER,
        reason="thread_echo",
    )

    card = chat.thread_card(seeded.thread)
    await chat.wait_for_card_state(seeded.thread, "done")
    await expect(card.locator(chat.THREAD_CARD_STATE)).to_have_text("Done")
    await expect(card).to_contain_text("Acme is cheapest at $2,400")
    await expect(
        chat.sidebar_group("done").get_by_test_id(f"thread-item-{seeded.thread}")
    ).to_be_visible()
    echoed = chat.echo_of(seeded.thread)
    await expect(echoed).to_contain_text(
        "Acme is cheapest at $2,400; booked for Tuesday.", timeout=15000
    )
    await expect(echoed.get_by_test_id("thread-echo-header")).to_have_text(
        "From thread: Hot water system quotes"
    )

    # Something new arriving in the thread reopens it.
    await _add_thread_reply(db, seeded, "Acme asked to move to Wednesday. Is that OK?")
    await _hub(web_test_fixture).publish_activity(
        seeded.thread, user_id=USER, reason="message"
    )

    await chat.wait_for_card_state(seeded.thread, "waiting")
    await expect(card).to_contain_text("Acme asked to move to Wednesday")
    await expect(
        chat.sidebar_group("waiting").get_by_test_id(f"thread-item-{seeded.thread}")
    ).to_be_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_opening_a_thread_clears_its_badge_and_it_can_be_marked_done(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    seeded = await _seed_thread(db, home)
    await web_test_fixture.page.set_viewport_size(WIDE_VIEWPORT)
    chat = await _open_home(web_test_fixture, home)
    await _finish_first_run_with_question(web_test_fixture, db, seeded)
    await chat.wait_for_card_state(seeded.thread, "waiting")

    card = chat.thread_card(seeded.thread)
    await expect(card.locator(chat.THREAD_UNREAD_BADGE)).to_have_text("1")
    await expect(
        chat.sidebar_thread(seeded.thread).locator(chat.THREAD_UNREAD_BADGE)
    ).to_have_text("1")

    page = web_test_fixture.page
    async with page.expect_response(
        lambda response: (
            f"/api/v1/chat/conversations/{seeded.thread}/read" in response.url
            and response.ok
        ),
        timeout=15000,
    ):
        await chat.open_thread_from_card(seeded.thread)
    await expect(
        chat.sidebar_thread(seeded.thread).locator(chat.THREAD_UNREAD_BADGE)
    ).to_have_count(0)
    [summary] = await _home_threads(web_test_fixture.base_url)
    assert summary["unread_count"] == 0

    banner = chat.thread_banner()
    mark_done = banner.locator(chat.THREAD_MARK_DONE)
    # Mark done stays reachable on the narrowest phone screen.
    desktop = page.viewport_size
    assert desktop is not None
    await page.set_viewport_size({"width": 320, "height": 640})
    await expect(mark_done).to_be_in_viewport(ratio=1)
    assert not await page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )
    await page.set_viewport_size(desktop)

    await mark_done.click()
    await expect(banner).to_have_attribute("data-thread-state", "done")
    await expect(banner.locator(chat.THREAD_BANNER_STATE)).to_have_text(
        "You marked this done"
    )
    await expect(banner.locator(chat.THREAD_MARK_DONE)).to_have_count(0)

    await chat.close_panel()
    await chat.wait_for_home(home)
    await chat.wait_for_card_state(seeded.thread, "done")
    await expect(card.locator(chat.THREAD_CARD_STATE)).to_have_text(
        "You marked this done"
    )
    await expect(card.locator(chat.THREAD_UNREAD_BADGE)).to_have_count(0)
    await expect(chat.echo_of(seeded.thread)).to_have_count(0)


GUTTER_QUESTION = "Is gutter guard worth it?"
GUTTER_REPLY = "Probably, for a roof under gum trees."


async def _seed_exchange(
    db: Database,
    conversation_id: str,
    *,
    profile_id: str,
    question: str,
    reply: str,
    turn_id: str | None = None,
) -> None:
    """A finished turn the user started: their question and its reply."""
    for message, user_id in (
        (UserMessage.from_trusted_user(content=question), USER),
        (AssistantMessage(content=reply), None),
    ):
        await db.message_history.add_message(
            message,
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
            processing_profile_id=profile_id,
            turn_id=turn_id,
            user_id=user_id,
        )


async def _seed_home_exchange(db: Database, home: str) -> None:
    await _seed_exchange(
        db,
        home,
        profile_id="default_assistant",
        question=GUTTER_QUESTION,
        reply=GUTTER_REPLY,
        turn_id=str(uuid.uuid4()),
    )


async def _seed_resting_thread(
    db: Database, home: str, *, profile_id: str, title: str
) -> str:
    """A thread with no run, waiting on the user after one exchange.

    The user's own message is what makes the thread theirs.
    """
    thread = f"web_conv_{uuid.uuid4()}"
    await db.conversation_threads.create_thread(
        conversation_id=thread,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=USER,
        profile_id=profile_id,
        title=title,
        title_taint=None,
        anchor_message_id=None,
        delegation_id=None,
        now=datetime.now(UTC),
    )
    await _seed_exchange(
        db,
        thread,
        profile_id=profile_id,
        question=f"Let's sort out: {title}.",
        reply=f"Ready when you are: {title}.",
    )
    return thread


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_a_new_thread_opens_in_the_panel_and_streams_its_first_reply(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    mock_llm_client.rules = [
        (
            lambda args: "Plan the veggie garden" in str(args.get("messages", [])),
            LLMOutput(content="Start with tomatoes and basil along the north fence."),
        ),
    ]
    home = await _home_conversation_id(web_test_fixture.base_url)
    await web_test_fixture.page.set_viewport_size(WIDE_VIEWPORT)
    chat = await _open_home(web_test_fixture, home)
    page = web_test_fixture.page

    await page.get_by_test_id("new-thread-button").click()
    await chat.start_thread(THREAD_PROFILE, "Plan the veggie garden")

    panel = chat.side_pane()
    await expect(panel.get_by_text("Start with tomatoes and basil")).to_be_visible(
        timeout=20000
    )
    await expect(
        panel.locator(chat.MESSAGE_USER).filter(has_text="Plan the veggie garden")
    ).to_be_visible()
    await expect(panel.locator(chat.CONVERSATION_HEADING)).to_have_text(
        "Plan the veggie garden"
    )
    await expect(chat.pane(home)).to_be_visible()
    [started] = await _home_threads(web_test_fixture.base_url)
    assert started["profile_id"] == THREAD_PROFILE
    await expect(panel).to_have_attribute(
        "data-pane-conversation-id", started["conversation_id"]
    )
    await expect(chat.sidebar_thread(started["conversation_id"])).to_be_visible()
    await expect(
        chat
        .pane(home)
        .locator(chat.MESSAGE_USER)
        .filter(has_text="Plan the veggie garden")
    ).to_have_count(0)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_continue_in_a_thread_with_context_shows_the_copied_turn_and_where_it_started(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    mock_llm_client.rules = [
        (
            lambda args: "Which brand lasts longest?" in str(args.get("messages", [])),
            LLMOutput(content="Aluminium mesh outlasts the plastic kinds."),
        ),
    ]
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    await _seed_home_exchange(db, home)
    await web_test_fixture.page.set_viewport_size(WIDE_VIEWPORT)
    chat = await _open_home(web_test_fixture, home)

    reply = chat.reply_in(home, GUTTER_REPLY)
    await expect(reply).to_be_visible(timeout=15000)
    await chat.message_action(reply, "continue-in-thread")
    await chat.start_thread(
        THREAD_PROFILE, "Which brand lasts longest?", with_context=True
    )

    panel = chat.side_pane()
    await expect(
        panel.get_by_text("Aluminium mesh outlasts the plastic kinds.")
    ).to_be_visible(timeout=20000)
    [started] = await _home_threads(web_test_fixture.base_url)
    started_thread = started["conversation_id"]
    assert started["anchor_message_id"] is not None
    await expect(
        panel.locator(chat.MESSAGE_USER).filter(has_text=GUTTER_QUESTION)
    ).to_be_visible()
    await expect(chat.reply_in(started_thread, GUTTER_REPLY)).to_be_visible()
    await expect(panel.locator(chat.THREAD_ANCHOR_QUOTE)).to_have_text(
        f"“{GUTTER_REPLY}”"
    )
    # Continuing copies; Home keeps its own exchange.
    await expect(reply).to_be_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_a_home_turn_moves_into_a_thread_only_where_context_is_received(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    research = await _seed_resting_thread(
        db, home, profile_id=THREAD_PROFILE, title="Roof maintenance"
    )
    browsing = await _seed_resting_thread(
        db, home, profile_id="test_browser", title="Book the campsite"
    )
    await _seed_home_exchange(db, home)
    await web_test_fixture.page.set_viewport_size(WIDE_VIEWPORT)
    chat = await _open_home(web_test_fixture, home)

    reply = chat.reply_in(home, GUTTER_REPLY)
    await expect(reply).to_be_visible(timeout=15000)
    await chat.message_action(reply, "move-to-thread")

    incompatible = chat.move_option(browsing)
    await expect(incompatible).to_be_disabled()
    await expect(incompatible).to_contain_text(
        "Test_browser doesn't receive household context"
    )
    compatible = chat.move_option(research)
    await expect(compatible).to_be_enabled()
    await compatible.click()
    dialog = web_test_fixture.page.locator(chat.MOVE_TO_THREAD_DIALOG)
    await dialog.get_by_test_id("move-to-thread-submit").click()
    await expect(dialog).to_have_count(0, timeout=15000)

    marker = chat.pane(home).locator(chat.MOVED_TURN_MARKER)
    await expect(marker).to_have_text("Moved to Roof maintenance", timeout=15000)
    await expect(reply).to_have_count(0)
    await expect(
        chat.pane(home).locator(chat.MESSAGE_USER).filter(has_text=GUTTER_QUESTION)
    ).to_have_count(0)
    # The sidebar picked the move up: the thread's latest message is the reply.
    await expect(chat.sidebar_thread(research)).to_contain_text(
        GUTTER_REPLY, timeout=15000
    )

    await marker.get_by_test_id("moved-turn-link").click()
    panel = chat.pane(research)
    await expect(panel).to_have_attribute("data-presentation", "side")
    await expect(
        panel.locator(chat.MESSAGE_USER).filter(has_text=GUTTER_QUESTION)
    ).to_be_visible(timeout=15000)
    await expect(chat.reply_in(research, GUTTER_REPLY)).to_be_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_a_move_is_not_offered_when_no_thread_receives_household_context(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    browsing = await _seed_resting_thread(
        db, home, profile_id="test_browser", title="Book the campsite"
    )
    await _seed_home_exchange(db, home)
    chat = await _open_home(web_test_fixture, home)

    reply = chat.reply_in(home, GUTTER_REPLY)
    await expect(reply).to_be_visible(timeout=15000)
    await chat.message_action(reply, "move-to-thread")

    dialog = web_test_fixture.page.locator(chat.MOVE_TO_THREAD_DIALOG)
    await expect(chat.move_option(browsing)).to_be_disabled()
    await expect(dialog.get_by_test_id("move-to-thread-unavailable")).to_be_visible()
    await expect(dialog.get_by_test_id("move-to-thread-submit")).to_be_disabled()
