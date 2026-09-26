"""The review over a conversation nobody hand-wrote.

Every other test of the review seeds its source conversation through the
message-history repository, which is fast to read but proves nothing about the
provenance a *turn* actually records. The authorship rule is only as good as
the stamps real turns leave, so these tests produce the
conversation the way production does: ``handle_chat_interaction`` on a real
``ProcessingService``, with a fake model and real tools.

What that pins, in the order the design's cases run:

- a plain turn and a turn that calls a trusted-output tool leave a stretch the
  review reads;
- a turn that called a tool tagged ``output_untrusted`` contributes only the
  household's own words to the review;
- and the rows carry first-hand stamps in every case.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import NotesContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import text_content
from family_assistant.memory.index import strip_topic_index
from family_assistant.memory.review import MemoryReviewResult, run_memory_review
from family_assistant.memory.review_settings import MemoryReviewSettings
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import (
    SourceTrustTier,
    TurnTaintState,
    is_externally_authored,
    merge_history_taint,
)
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS, LocalToolsProvider
from family_assistant.tools.infrastructure import TaintTrackingToolsProvider
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.types import ToolDefinition, ToolExecutionContext
from family_assistant.utils.clock import MockClock
from tests.functional.memory.curator_harness import (
    CONTRIBUTOR,
    CONVERSATION,
    CURATOR_READ_POLICY,
    NOW,
    CuratorScript,
    curator_llm,
    curator_service,
    enable_contribution,
    memory_db,
    review_limits,
)
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.memory.limits import MemoryLimits
    from family_assistant.storage.types import MessageHistoryRow

SPOKE_AT = NOW - timedelta(minutes=45)
"""When the source turns happen: after contribution was turned on, before now."""

SETTINGS = MemoryReviewSettings(
    idle_window_minutes={"web": 30, "telegram": 30},
    contributing_interfaces=frozenset({"web", "telegram"}),
)

SAID = "we always take the tram, never the bus"


# ---------------------------------------------------------------------------
# A contributing profile, as a deployment configures one
# ---------------------------------------------------------------------------


async def _read_the_open_web(**_kwargs: object) -> str:
    """A tool whose output the household does not control."""
    return "Bin day has moved to Tuesday, says the council page."


OPEN_WEB = ToolRegistration(
    definition=cast(
        "ToolDefinition",
        {
            "type": "function",
            "function": {
                "name": "read_open_web",
                "description": "Read a page the household does not control.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ),
    implementation=_read_the_open_web,
    metadata=make_local_tool_metadata([ToolTag.READ_ONLY, ToolTag.OUTPUT_UNTRUSTED]),
)


def contributor_service(
    engine: AsyncEngine, llm: RuleBasedMockLLMClient
) -> ProcessingService:
    """The profile the reviewed conversation runs under, with real tools.

    Wrapped in ``TaintTrackingToolsProvider`` because that is what stamps a
    tool result with the trust tier its tags declare; a test that dropped it
    would never see an untrusted turn at all.
    """
    config = ProcessingServiceConfig(
        id=CONTRIBUTOR,
        prompts={"system_prompt": "You are the household assistant."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=20,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.BLOCKED,
        memory_read=True,
        memory_contribute=True,
        memory_contributing_interfaces=frozenset({"web", "telegram"}),
    )
    read_policy = NoteReadPolicy.for_profile(
        visibility_grants=None, required_labels=None, memory_read=True
    )
    return ProcessingService(
        llm_client=llm,
        tools_provider=TaintTrackingToolsProvider(
            LocalToolsProvider(registrations=[*LOCAL_TOOL_REGISTRATIONS, OPEN_WEB])
        ),
        service_config=config,
        context_providers=[
            NotesContextProvider(
                get_db_context_func=lambda: Database(engine=engine),
                prompts=config.prompts,
                read_policy=read_policy,
            )
        ],
        server_url=None,
        app_config=AppConfig(),
        clock=MockClock(SPOKE_AT),
    )


def _before_any_tool_ran(args: MatcherArgs) -> bool:
    """Whether this is the model's first call of the turn."""
    return not any(
        getattr(message, "role", "") == "tool" for message in args["messages"]
    )


def _calls(name: str, arguments: str = "{}") -> LLMOutput:
    return LLMOutput(
        content="",
        tool_calls=[
            ToolCallItem(
                id=f"call_{uuid.uuid4()}",
                type="function",
                function=ToolCallFunction(name=name, arguments=arguments),
            )
        ],
    )


async def _say(
    service: ProcessingService,
    engine: AsyncEngine,
    *,
    said: str,
    interface_type: str = "web",
    message_id: str = "1",
) -> None:
    """One real turn, start to finish, the way an interface drives one."""
    result = await service.handle_chat_interaction(
        db_context=Database(engine=engine),
        chat_interface=MagicMock(),
        interface_type=interface_type,
        conversation_id=CONVERSATION,
        trigger_content_parts=[text_content(said)],
        trigger_interface_message_id=message_id,
        user_name="alice",
        user_id="alice",
    )
    assert result.error_traceback is None, result.error_traceback


# ---------------------------------------------------------------------------
# Running the review over what those turns left
# ---------------------------------------------------------------------------


def _context(db: Database, service: ProcessingService) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="internal",
        conversation_id="memory-review",
        user_name="system",
        turn_id=None,
        db_context=db,
        processing_service=service,
        clock=MockClock(NOW),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=None,
        timezone=service.service_config.timezone,
        credential_resolvers=None,
        api_backend=None,
    )


async def _review(
    db: Database,
    curator: ProcessingService,
    *,
    limits: MemoryLimits,
    interface_type: str = "web",
) -> MemoryReviewResult:
    return await run_memory_review(
        _context(db, curator),
        interface_type=interface_type,
        conversation_id=CONVERSATION,
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
        limits=limits,
    )


async def _rows(db: Database) -> Sequence[MessageHistoryRow]:
    return await db.message_history.rows_matching(
        message_history_table.c.conversation_id == CONVERSATION
    )


# ---------------------------------------------------------------------------
# (a) and (b): a trusted conversation is reviewable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("interface_type", ["web", "telegram"])
async def test_a_conversation_of_real_turns_becomes_a_memory_entry(
    db_engine: AsyncEngine, interface_type: str
) -> None:
    """The headline: nothing about a real trusted turn trips the provenance rule.

    Both household interfaces, because they reach the same entry point by
    different routes and the stamp is what has to match, not the route.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    await enable_contribution(db)

    assistant = contributor_service(
        db_engine,
        RuleBasedMockLLMClient(rules=[(lambda _a: True, LLMOutput(content="Noted."))]),
    )
    await _say(assistant, db_engine, said=SAID, interface_type=interface_type)

    script = CuratorScript()
    curator = curator_service(db_engine, curator_llm(script))
    result = await _review(db, curator, limits=limits, interface_type=interface_type)

    assert result is MemoryReviewResult.APPLIED
    note = await db.notes.get_by_title(
        script.note_title, read_policy=CURATOR_READ_POLICY
    )
    assert note is not None
    assert "takes the tram" in strip_topic_index(note.content)


@pytest.mark.asyncio
async def test_a_turn_that_called_a_trusted_tool_is_still_reviewable(
    db_engine: AsyncEngine,
) -> None:
    """A note written mid-turn taints nothing: its output is the household's."""
    limits = review_limits()
    db = memory_db(db_engine, limits)
    await enable_contribution(db)

    assistant = contributor_service(
        db_engine,
        RuleBasedMockLLMClient(
            rules=[
                (
                    _before_any_tool_ran,
                    _calls(
                        "add_or_update_note",
                        json.dumps({"title": "Transport", "content": SAID}),
                    ),
                ),
                (lambda _a: True, LLMOutput(content="Saved that.")),
            ]
        ),
    )
    await _say(assistant, db_engine, said=f"note that {SAID}")

    curator = curator_service(db_engine, curator_llm(CuratorScript()))
    assert await _review(db, curator, limits=limits) is MemoryReviewResult.APPLIED


# ---------------------------------------------------------------------------
# (c): a turn that read the open web retains its household-authored message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_that_read_the_open_web_curates_only_the_users_words(
    db_engine: AsyncEngine,
) -> None:
    """The curator sees the household preference without the web result."""
    limits = review_limits()
    db = memory_db(db_engine, limits)
    await enable_contribution(db)

    assistant = contributor_service(
        db_engine,
        RuleBasedMockLLMClient(
            rules=[
                (_before_any_tool_ran, _calls("read_open_web")),
                (lambda _a: True, LLMOutput(content="Bin day is Tuesday.")),
            ]
        ),
    )
    await _say(assistant, db_engine, said=SAID)

    curator_llm_client = curator_llm(CuratorScript())
    curator = curator_service(db_engine, curator_llm_client)
    result = await _review(db, curator, limits=limits)

    assert result is MemoryReviewResult.APPLIED
    calls = curator_llm_client.get_calls()
    assert calls
    assert SAID in str(calls)
    assert "Bin day is Tuesday" not in str(calls)
    assert "Bin day has moved" not in str(calls)


@pytest.mark.asyncio
async def test_tainted_foreground_memory_request_defers_to_review(
    db_engine: AsyncEngine,
) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    await enable_contribution(db)
    proposal = json.dumps({
        "edits": [
            {
                "op": "add",
                "note_title": "Transport",
                "entry": SAID,
            }
        ]
    })
    assistant = contributor_service(
        db_engine,
        RuleBasedMockLLMClient(
            rules=[
                (_before_any_tool_ran, _calls("read_open_web")),
                (
                    lambda args: (
                        sum(message.role == "tool" for message in args["messages"]) == 1
                    ),
                    _calls("propose_memory_edits", proposal),
                ),
                (
                    lambda _args: True,
                    LLMOutput(content="I will leave that for review."),
                ),
            ]
        ),
    )
    await _say(assistant, db_engine, said=f"remember that {SAID}")

    tool_rows = [row for row in await _rows(db) if row["role"] == "tool"]
    assert any(
        "eligible for a later memory review" in str(row["content"]) for row in tool_rows
    )
    assert (
        await db.notes.get_by_title("Transport", read_policy=CURATOR_READ_POLICY)
        is None
    )

    curator = curator_service(
        db_engine, curator_llm(CuratorScript(note_title="Transport"))
    )
    assert await _review(db, curator, limits=limits) is MemoryReviewResult.APPLIED
    note = await db.notes.get_by_title("Transport", read_policy=CURATOR_READ_POLICY)
    assert note is not None
    assert "takes the tram" in strip_topic_index(note.content)


# ---------------------------------------------------------------------------
# Why the two cases differ: the stamps the turns left
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_row_a_real_turn_writes_carries_its_own_stamp(
    db_engine: AsyncEngine,
) -> None:
    """No row of a real turn relies on the read-time unknown-external fallback.

    That fallback exists for rows written before runtime taint tracking, and it
    is deliberately fail-closed. If a turn stopped stamping one of its rows,
    every stretch containing it would be skipped on provenance and the review
    would quietly stop learning anything; this is what would catch that.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    assistant = contributor_service(
        db_engine,
        RuleBasedMockLLMClient(
            rules=[
                (
                    _before_any_tool_ran,
                    _calls(
                        "add_or_update_note",
                        json.dumps({"title": "Transport", "content": SAID}),
                    ),
                ),
                (lambda _a: True, LLMOutput(content="Saved that.")),
            ]
        ),
    )
    await _say(assistant, db_engine, said=f"note that {SAID}")

    rows = await _rows(db)
    assert [row["role"] for row in rows] == ["user", "assistant", "tool", "assistant"]
    for row in rows:
        metadata = row["taint_metadata"]
        assert metadata is not None, f"{row['role']} row left unstamped"
        assert "legacy_missing_taint_metadata" not in json.dumps(metadata), (
            f"{row['role']} row fell back to the legacy unknown-external default"
        )
    merged = merge_history_taint([
        MagicMock(taint_metadata=row["taint_metadata"]) for row in rows
    ])
    assert not is_externally_authored(merged.max_tier)


@pytest.mark.asyncio
async def test_user_authorship_does_not_inherit_tainted_history(
    db_engine: AsyncEngine,
) -> None:
    researched = contributor_service(
        db_engine,
        RuleBasedMockLLMClient(
            rules=[
                (_before_any_tool_ran, _calls("read_open_web")),
                (lambda _a: True, LLMOutput(content="Outside result.")),
            ]
        ),
    )
    await _say(researched, db_engine, said="Find a hotel", message_id="first")
    followed_up = contributor_service(
        db_engine,
        RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content="Noted.")),
    )
    await _say(followed_up, db_engine, said=SAID, message_id="second")

    rows = await _rows(Database(engine=db_engine))
    assert [row["role"] for row in rows[-2:]] == ["user", "assistant"]
    assert (
        TurnTaintState.from_metadata(rows[-2]["taint_metadata"]).max_tier
        is SourceTrustTier.TRUSTED_USER
    )
    assert (
        TurnTaintState.from_metadata(rows[-1]["taint_metadata"]).max_tier
        is SourceTrustTier.UNKNOWN_EXTERNAL
    )
