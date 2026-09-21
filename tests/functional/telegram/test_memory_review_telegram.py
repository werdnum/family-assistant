"""Memory review over a real Telegram chat.

Work-plan item 4 of docs/design/conversation-memory.md, "Telegram: sender names
and maximum deferral". Telegram needs no separate review mechanism, but it is
what three of the rules exist for, and only rows a real Telegram turn wrote can
say whether those rules hold on it:

- **Per-person attribution.** A group chat is one conversation with several
  speakers, so the transcript has to name each one. The names here come from
  the deployment's ``users`` configuration keyed on the id the interface
  persisted -- deliberately *not* from the display name on the update, which is
  spent on the system prompt and never stored. Both senders below arrive as
  "TestUser", and the curator still sees Alice and Bob.
- **The longer idle window.** Ninety minutes, because a member replying twenty
  minutes later is still the same exchange.
- **The maximum deferral**, because a chat id never ends: a chat that is still
  active, and so never idle, is reviewed anyway once its oldest unreviewed row
  has waited a day.

The conversation is produced by ``TelegramUpdateHandler`` with a fake model;
the curator is the shipped profile with a fake model. Nothing about the
persistence, the eligibility or the rendering is stood in for.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.memory.due import DueReason, select_due_conversations
from family_assistant.memory.index import strip_topic_index
from family_assistant.memory.review import MemoryReviewResult, run_memory_review
from family_assistant.memory.review_settings import MemoryReviewSettings
from family_assistant.memory.sweep import run_memory_review_sweep
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage.message_history import message_history_table
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock
from tests.functional.memory.curator_harness import (
    CURATOR_READ_POLICY,
    REQUEST_MARKER,
    curator_service,
    memory_db,
    prompt_text,
    review_limits,
    tool_results,
)
from tests.functional.telegram.test_telegram_handler import (
    create_context,
    create_mock_update,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.memory.limits import MemoryLimits
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.database import Database
    from tests.functional.telegram.conftest import TelegramHandlerTestFixture
    from tests.mocks.mock_llm import MatcherArgs

PROFILE = "default_assistant_test_profile"
"""The profile the Telegram fixture's chat runs under."""

CHAT_ID = 123
CONVERSATION = str(CHAT_ID)
TELEGRAM = "telegram"

ALICE_TELEGRAM_ID = 12345
BOB_TELEGRAM_ID = 777
ALICE = "alice@example.com"
BOB = "bob@example.com"

SETTINGS = MemoryReviewSettings()
"""The shipped windows: 90 minutes idle on Telegram, 24 hours maximum."""

IDLE_MINUTES = 90
SPEAKER_LINE = re.compile(r"^#(\d+) \S+ \S+ ([^:]+): (.*)$", re.MULTILINE)


@pytest.fixture
# ast-grep-ignore: no-dict-any - config overrides mirror the raw AppConfig schema
def telegram_config_overrides() -> dict[str, Any]:
    """Two household members in one Telegram chat, each with a name.

    ``users`` is where a canonical user's display name lives, and it is the
    only place a review can learn one: it renders stored rows, and a stored row
    carries an id.
    """
    return {
        "users": [
            {
                "id": ALICE,
                "label": "Alice",
                "telegram": {"user_ids": [ALICE_TELEGRAM_ID]},
            },
            {"id": BOB, "label": "Bob", "telegram": {"user_ids": [BOB_TELEGRAM_ID]}},
        ]
    }


# ---------------------------------------------------------------------------
# A curator that attributes what it reads
# ---------------------------------------------------------------------------


@dataclass
class AttributingCurator:
    """A fake curator that records one speaker's line, in their name.

    It reads the speaker, the id and the text off the rendered transcript
    rather than being told them, so a review that renders the wrong sender
    produces the wrong entry -- or, if the transcript stops being parseable per
    speaker at all, no proposal and a failing test.
    """

    speaker: str
    requests: list[str] = field(default_factory=list)

    def rules(self) -> list[tuple[object, object]]:
        return [(self._matches, self._respond)]

    def _matches(self, args: MatcherArgs) -> bool:
        return REQUEST_MARKER in prompt_text(args["messages"])

    def _respond(self, args: MatcherArgs) -> LLMOutput:
        text = prompt_text(args["messages"])
        if tool_results(args["messages"]):
            return LLMOutput(content="Done.")
        self.requests.append(text)
        line = self._line_for_speaker(text)
        if line is None:
            return LLMOutput(content=f"Nothing from {self.speaker} in this stretch.")
        internal_id, said = line
        return LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id=f"call-{uuid.uuid4()}",
                    type="function",
                    function=ToolCallFunction(
                        name="propose_memory_edits",
                        arguments=json.dumps({
                            "edits": [
                                {
                                    "op": "add",
                                    "note_title": "Transport",
                                    "entry": f"{self.speaker} said that {said}",
                                    "message_ids": [internal_id],
                                }
                            ]
                        }),
                    ),
                )
            ],
        )

    def _line_for_speaker(self, text: str) -> tuple[int, str] | None:
        """The first transcript line this speaker's name is on."""
        for match in SPEAKER_LINE.finditer(text):
            if match.group(2) == self.speaker:
                return int(match.group(1)), match.group(3)
        return None


def _curator(
    engine: AsyncEngine, script: AttributingCurator
) -> tuple[ProcessingService, AttributingCurator]:
    client = RuleBasedMockLLMClient(
        # type: ignore is the harness's own convention here: the rule tuple
        # carries its matcher and generator types, which the client types more
        # loosely than this dataclass declares them.
        rules=script.rules(),  # type: ignore[arg-type]
        default_response=LLMOutput(content="No memory review request was recognised."),
    )
    return curator_service(engine, client), script


# ---------------------------------------------------------------------------
# Driving the chat and the review
# ---------------------------------------------------------------------------


async def _send(
    fixture: TelegramHandlerTestFixture,
    *,
    text: str,
    telegram_user_id: int,
    message_id: int,
) -> None:
    """One real Telegram turn, start to finish."""
    update = create_mock_update(
        text, chat_id=CHAT_ID, user_id=telegram_user_id, message_id=message_id
    )
    context = create_context(
        fixture.application, chat_id=CHAT_ID, user_id=telegram_user_id
    )
    await fixture.handler.message_handler(update, context)


def _context(
    db: Database, curator: ProcessingService, *, now: datetime
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="internal",
        conversation_id="memory-review",
        user_name="system",
        turn_id=None,
        db_context=db,
        processing_service=curator,
        clock=MockClock(now),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=None,
        timezone=curator.service_config.timezone,
        credential_resolvers=None,
        api_backend=None,
    )


async def _review(
    db: Database,
    curator: ProcessingService,
    *,
    limits: MemoryLimits,
    now: datetime,
    resolver: UserIdentityResolver,
) -> MemoryReviewResult:
    return await run_memory_review(
        _context(db, curator, now=now),
        interface_type=TELEGRAM,
        conversation_id=CONVERSATION,
        settings=SETTINGS,
        configured_contributors={PROFILE},
        limits=limits,
        name_for_user_id=resolver.label_for_stored_user_id,
    )


async def _sweep(
    db: Database, curator: ProcessingService, *, now: datetime
) -> list[DueReason]:
    """Run the sweep and report why each conversation it enqueued was due."""
    due = await select_due_conversations(
        db,
        now=now,
        settings=SETTINGS,
        contributing_profiles=await _contributing(db),
    )
    enqueued = await run_memory_review_sweep(
        _context(db, curator, now=now),
        settings=SETTINGS,
        configured_contributors={PROFILE},
    )
    assert enqueued == len(due)
    return [conversation.reason for conversation in due]


async def _contributing(db: Database) -> dict[str, datetime]:
    enablement = await db.memory_review.get_enablement()
    return {
        profile_id: enabled_at
        for profile_id, enabled_at in enablement.items()
        if profile_id == PROFILE
    }


async def _user_ids(db: Database) -> list[str | None]:
    rows = await db.message_history.rows_matching(
        message_history_table.c.conversation_id == CONVERSATION
    )
    return [row["user_id"] for row in rows if row["role"] == "user"]


async def _seed_turn(
    db: Database,
    *,
    said: str,
    at: datetime,
    speaker: str,
    turn_id: str,
    profile_id: str = PROFILE,
) -> list[int]:
    """One turn, written the way a turn writes one. Returns its row ids.

    Used where a test needs a turn the Telegram fixture cannot produce: one
    older than the test run itself, or one under a profile a slash command
    switched to. The stamps match what a real turn leaves, so nothing is
    skipped on provenance.
    """
    trusted = TurnTaintState.empty().to_metadata()
    return [
        await db.message_history.add_message(
            message,
            interface_type=TELEGRAM,
            conversation_id=CONVERSATION,
            timestamp=at,
            turn_id=turn_id,
            processing_profile_id=profile_id,
            user_id=speaker,
        )
        for message in (
            UserMessage(content=said, taint_metadata=trusted),
            AssistantMessage(content="Noted.", taint_metadata=trusted),
        )
    ]


async def _watermark(db: Database) -> int:
    row = await db.memory_review.get_watermark(
        interface_type=TELEGRAM, conversation_id=CONVERSATION
    )
    return row.last_reviewed_internal_id if row is not None else 0


# ---------------------------------------------------------------------------
# Sender names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_telegram_senders_are_rendered_under_their_own_names(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """The headline: one chat, two speakers, each named in the transcript.

    And the entry the curator writes credits the right one, citing the id it
    read off that speaker's own line -- so the attribution is checked through
    the apply path's evidence scope rather than against a string.
    """
    fixture = telegram_handler_fixture
    limits = review_limits()
    db = memory_db(db_engine, limits)
    resolver = UserIdentityResolver(fixture.assistant.config)
    started = datetime.now(UTC)
    await db.memory_review.record_enablement(
        profile_ids_contributing={PROFILE}, now=started - timedelta(days=1)
    )

    await _send(
        fixture,
        text="we always take the tram",
        telegram_user_id=ALICE_TELEGRAM_ID,
        message_id=101,
    )
    await _send(
        fixture,
        text="the 7pm tram is the last one",
        telegram_user_id=BOB_TELEGRAM_ID,
        message_id=102,
    )

    assert await _user_ids(db) == [ALICE, BOB]

    curator, script = _curator(db_engine, AttributingCurator(speaker="Bob"))
    settled = started + timedelta(minutes=IDLE_MINUTES + 1)
    assert await _sweep(db, curator, now=settled) == [DueReason.IDLE]
    result = await _review(db, curator, limits=limits, now=settled, resolver=resolver)

    assert result is MemoryReviewResult.APPLIED
    assert "Alice: we always take the tram" in script.requests[0]
    assert "Bob: the 7pm tram is the last one" in script.requests[0]
    note = await db.notes.get_by_title("Transport", read_policy=CURATOR_READ_POLICY)
    assert note is not None
    assert "Bob said that the 7pm tram is the last one" in strip_topic_index(
        note.content
    )


@pytest.mark.asyncio
async def test_a_telegram_chat_is_not_reviewed_while_it_is_still_within_its_window(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """Forty-five minutes of quiet is a pause on Telegram, not the end."""
    fixture = telegram_handler_fixture
    limits = review_limits()
    db = memory_db(db_engine, limits)
    started = datetime.now(UTC)
    await db.memory_review.record_enablement(
        profile_ids_contributing={PROFILE}, now=started - timedelta(days=1)
    )

    await _send(
        fixture,
        text="we always take the tram",
        telegram_user_id=ALICE_TELEGRAM_ID,
        message_id=101,
    )

    curator, _ = _curator(db_engine, AttributingCurator(speaker="Alice"))
    assert await _sweep(db, curator, now=started + timedelta(minutes=45)) == []


# ---------------------------------------------------------------------------
# Maximum deferral
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_telegram_chat_that_never_goes_quiet_is_reviewed_anyway(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """A chat id never ends, so idleness alone would never review this one.

    Messages keep landing well inside the ninety-minute window -- the last of
    them a real turn seconds ago -- and the chat is reviewed because its oldest
    unreviewed row has waited past the maximum deferral.
    """
    fixture = telegram_handler_fixture
    limits = review_limits()
    db = memory_db(db_engine, limits)
    resolver = UserIdentityResolver(fixture.assistant.config)
    now = datetime.now(UTC)
    await db.memory_review.record_enablement(
        profile_ids_contributing={PROFILE}, now=now - timedelta(days=7)
    )

    for hours_ago in (30, 28, 26, 24, 2, 1):
        await _seed_turn(
            db,
            said=f"still talking, {hours_ago} hours ago",
            at=now - timedelta(hours=hours_ago),
            speaker=ALICE,
            turn_id=f"turn-{hours_ago}",
        )
    await _send(
        fixture,
        text="we always take the tram",
        telegram_user_id=BOB_TELEGRAM_ID,
        message_id=101,
    )

    curator, script = _curator(db_engine, AttributingCurator(speaker="Bob"))
    assert await _sweep(db, curator, now=now) == [DueReason.MAX_DEFERRAL]
    result = await _review(db, curator, limits=limits, now=now, resolver=resolver)

    assert result is MemoryReviewResult.APPLIED
    assert "Bob: we always take the tram" in script.requests[0]


# ---------------------------------------------------------------------------
# A profile switched by slash command inside the chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_profile_switched_mid_stretch_is_neither_shown_nor_left_behind(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """`/engineer` inside a household chat is handled by eligibility alone.

    Its rows are not rendered, and the watermark still moves past them, so they
    cannot hold the conversation's own stretch behind them for ever.
    """
    fixture = telegram_handler_fixture
    limits = review_limits()
    db = memory_db(db_engine, limits)
    resolver = UserIdentityResolver(fixture.assistant.config)
    started = datetime.now(UTC)
    await db.memory_review.record_enablement(
        profile_ids_contributing={PROFILE}, now=started - timedelta(days=1)
    )

    await _send(
        fixture,
        text="we always take the tram",
        telegram_user_id=ALICE_TELEGRAM_ID,
        message_id=101,
    )
    diagnosed = await _seed_turn(
        db,
        said="why did the daily brief not fire last night",
        at=started,
        speaker=ALICE,
        turn_id="engineer-turn",
        profile_id="engineer",
    )
    await _send(
        fixture,
        text="the 7pm tram is the last one",
        telegram_user_id=BOB_TELEGRAM_ID,
        message_id=102,
    )

    curator, script = _curator(db_engine, AttributingCurator(speaker="Bob"))
    settled = started + timedelta(minutes=IDLE_MINUTES + 1)
    result = await _review(db, curator, limits=limits, now=settled, resolver=resolver)

    assert result is MemoryReviewResult.APPLIED
    assert "daily brief" not in script.requests[0]
    assert "Alice: we always take the tram" in script.requests[0]
    assert "Bob: the 7pm tram is the last one" in script.requests[0]
    assert await _watermark(db) > max(diagnosed)


@pytest.mark.asyncio
async def test_a_telegram_chat_that_only_used_another_profile_is_not_due(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """A row under `/engineer` is not this chat's activity, however old it is."""
    del telegram_handler_fixture
    limits = review_limits()
    db = memory_db(db_engine, limits)
    now = datetime.now(UTC)
    await db.memory_review.record_enablement(
        profile_ids_contributing={PROFILE}, now=now - timedelta(days=7)
    )
    await _seed_turn(
        db,
        said="why did the daily brief not fire last night",
        at=now - timedelta(days=2),
        speaker=ALICE,
        turn_id="engineer-turn",
        profile_id="engineer",
    )

    curator, _ = _curator(db_engine, AttributingCurator(speaker="Alice"))
    assert await _sweep(db, curator, now=now) == []
