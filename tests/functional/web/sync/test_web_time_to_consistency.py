"""How long the web client takes to show a change, on good and bad networks.

Each scenario commits a change at a known instant (or ends an outage at one)
and measures until the browser shows it. With ``SYNC_BENCHMARK=1`` every
scenario runs against every network profile several times and the results are
written to ``test-results/`` as a table; run that with ``-n 0`` so one process
collects all of them. Without it, a small subset runs once on the good profile
as a regression check with generous bounds.

    SYNC_BENCHMARK=1 pytest tests/functional/web/sync -n 0 --db sqlite -q -s

See docs/design/mobile-state-sync.md, "Measurement and test infrastructure".
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from tests.functional.web.pages.chat_page import ChatPage
from tests.functional.web.sync.benchmark import (
    ScenarioResult,
    full_run_requested,
    runs_per_scenario,
)
from tests.functional.web.sync.conftest import TurnDriver, reply_marker
from tests.helpers import wait_for_condition
from tests.netem.profiles import PROFILES
from tests.netem.proxy import ImpairedNetworkProxy

_FULL = full_run_requested()
_PROFILE_NAMES = list(PROFILES) if _FULL else ["good"]
_SCENARIOS = (
    [
        "cold_load",
        "commit_during_load",
        "connected_idle",
        "own_send",
        "after_blackhole",
        "after_handover",
        "other_conversation",
    ]
    if _FULL
    else ["connected_idle", "after_handover"]
)
_DEADLINE_S = 60.0 if _FULL else 30.0
_BLACKHOLE_S = 20.0 if _FULL else 3.0
# The regression subset runs on a perfect link, so anything slower than this
# is a broken client rather than a slow network.
_REGRESSION_BOUND_S = 15.0

_TEXT_VISIBLE_JS = """
([selector, text]) => Array.from(document.querySelectorAll(selector))
    .some((element) => element.innerText.includes(text))
"""
_THREAD = '[data-testid="assistant-message-content"]'


async def _seen_at(
    page: Page, selector: str, text: str, deadline_s: float
) -> float | None:
    """``time.monotonic()`` when ``text`` is visible in ``selector``, or None."""
    try:
        await page.wait_for_function(
            _TEXT_VISIBLE_JS,
            arg=[selector, text],
            polling=50,
            timeout=deadline_s * 1000,
        )
    except PlaywrightTimeoutError:
        return None
    return time.monotonic()


async def _wait_until_following(
    hub: ConversationStreamHub, conversation_id: str
) -> None:
    """Wait until the page holds a live subscription to the conversation.

    The web client opens its follow stream a moment after it loads history, and
    a change committed in between is missed (``commit_during_load`` measures
    that race). Steady-state scenarios start once the client is listening.
    """
    await wait_for_condition(
        lambda: hub.subscriber_count(conversation_id) > 0,
        timeout=_DEADLINE_S,
        description="the page's follow stream to subscribe",
    )


async def _reload_conversation(page: Page, url: str) -> bool:
    """Navigate under the current impairment; False if the page never loaded.

    Only the load scenarios do this. On a thin link the app's assets can take
    longer than the deadline, which is a result, not a harness failure.
    """
    try:
        await page.goto(url, wait_until="commit", timeout=_DEADLINE_S * 1000)
    except PlaywrightError:
        return False
    return True


def _elapsed(seen: float | None, since: float) -> float | None:
    return None if seen is None else max(0.0, seen - since)


class _Scenario:
    def __init__(
        self,
        page: Page,
        chat: ChatPage,
        proxy: ImpairedNetworkProxy,
        driver: TurnDriver,
        conversation_id: str,
        other_conversation_id: str,
        seed_token: str,
        hub: ConversationStreamHub,
    ) -> None:
        self.seed_token = seed_token
        self.hub = hub
        self.conversation_url = (
            f"{chat.base_url}/chat?conversation_id={conversation_id}"
        )
        self.page = page
        self.chat = chat
        self.proxy = proxy
        self.driver = driver
        self.conversation_id = conversation_id
        self.other_conversation_id = other_conversation_id

    async def cold_load(self, token: str) -> float | None:
        seed_marker = reply_marker(self.seed_token)
        started = time.monotonic()
        if not await _reload_conversation(self.page, self.conversation_url):
            return None
        return _elapsed(
            await _seen_at(self.page, _THREAD, seed_marker, _DEADLINE_S), started
        )

    async def commit_during_load(self, token: str) -> float | None:
        messages_path = f"/conversations/{self.conversation_id}/messages"
        try:
            async with self.page.expect_response(
                lambda r: messages_path in r.url, timeout=_DEADLINE_S * 1000
            ):
                if not await _reload_conversation(self.page, self.conversation_url):
                    return None
        except PlaywrightError:
            return None
        committed = await self.driver.complete_turn(self.conversation_id, token)
        seen = await _seen_at(self.page, _THREAD, reply_marker(token), _DEADLINE_S)
        return _elapsed(seen, committed)

    async def connected_idle(self, token: str) -> float | None:
        await _wait_until_following(self.hub, self.conversation_id)
        committed = await self.driver.complete_turn(self.conversation_id, token)
        seen = await _seen_at(self.page, _THREAD, reply_marker(token), _DEADLINE_S)
        return _elapsed(seen, committed)

    async def own_send(self, token: str) -> float | None:
        chat_input = self.page.locator(ChatPage.CHAT_INPUT)
        await chat_input.fill(f"bench {token}")
        started = time.monotonic()
        await chat_input.press("Enter")
        seen = await _seen_at(self.page, _THREAD, reply_marker(token), _DEADLINE_S)
        return _elapsed(seen, started)

    async def after_blackhole(self, token: str) -> float | None:
        await _wait_until_following(self.hub, self.conversation_id)
        self.proxy.blackhole(_BLACKHOLE_S)
        outage_ends = time.monotonic() + _BLACKHOLE_S
        committed = await self.driver.complete_turn(self.conversation_id, token)
        seen = await _seen_at(
            self.page, _THREAD, reply_marker(token), _BLACKHOLE_S + _DEADLINE_S
        )
        return _elapsed(seen, max(committed, outage_ends))

    async def after_handover(self, token: str) -> float | None:
        await _wait_until_following(self.hub, self.conversation_id)
        self.proxy.reset_all_connections()
        committed = await self.driver.complete_turn(self.conversation_id, token)
        seen = await _seen_at(self.page, _THREAD, reply_marker(token), _DEADLINE_S)
        return _elapsed(seen, committed)

    async def other_conversation(self, token: str) -> float | None:
        await _wait_until_following(self.hub, self.conversation_id)
        committed = await self.driver.complete_turn(self.other_conversation_id, token)
        row = f'[data-conversation-id="{self.other_conversation_id}"]'
        seen = await _seen_at(self.page, row, reply_marker(token), _DEADLINE_S)
        return _elapsed(seen, committed)

    def runner(self, name: str) -> Callable[[str], Awaitable[float | None]]:
        return getattr(self, name)


@pytest.mark.playwright
@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
@pytest.mark.parametrize("scenario", _SCENARIOS)
async def test_web_client_time_to_consistency(
    scenario: str,
    profile_name: str,
    page: Page,
    impaired_proxy: ImpairedNetworkProxy,
    turn_driver: TurnDriver,
    stream_hub: ConversationStreamHub,
    benchmark_results: list[ScenarioResult],
) -> None:
    conversation_id = str(uuid.uuid4())
    other_conversation_id = str(uuid.uuid4())
    seed_token = uuid.uuid4().hex[:10]
    await turn_driver.complete_turn(conversation_id, seed_token)
    await turn_driver.complete_turn(other_conversation_id, uuid.uuid4().hex[:10])

    # The app loads over a good link and the profile applies from then on, as
    # for someone whose connection degrades with the app already open. The
    # load scenarios reload under the profile themselves.
    chat = ChatPage(page, base_url=f"http://127.0.0.1:{impaired_proxy.port}")
    await chat.navigate_to_chat(conversation_id)
    assert await _seen_at(page, _THREAD, reply_marker(seed_token), _DEADLINE_S), (
        "the seeded conversation never rendered through the proxy"
    )
    impaired_proxy.set_impairment(PROFILES[profile_name])

    scenario_runner = _Scenario(
        page,
        chat,
        impaired_proxy,
        turn_driver,
        conversation_id,
        other_conversation_id,
        seed_token,
        stream_hub,
    )
    run = scenario_runner.runner(scenario)
    result = ScenarioResult(
        client="web",
        scenario=scenario,
        profile=profile_name,
        deadline_s=_DEADLINE_S,
    )
    before_runs = replace(impaired_proxy.stats)
    for _ in range(runs_per_scenario()):
        result.add(await run(uuid.uuid4().hex[:10]))
    result.record_traffic(impaired_proxy.stats, since=before_runs)
    benchmark_results.append(result)

    if not _FULL:
        assert result.timeouts == 0, f"{scenario}: the client never caught up"
        p50 = result.percentile(0.5)
        assert p50 is not None
        assert p50 < _REGRESSION_BOUND_S, f"{scenario}: {p50:.1f}s to catch up"
