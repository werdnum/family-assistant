"""What a foreground "remember this" cites, and what it may not.

docs/design/conversation-memory.md: "their evidence scope is the current turn,
so a foreground edit cites the message in which the person asked". The
foreground assistant is never shown a ``message_history.internal_id`` -- the
turn reaches it as prose, and only the review transcript renders ids -- so the
citation is bound from the running turn rather than asked of the model.

These drive a whole turn through ``handle_chat_interaction`` with a fake model,
because the question is precisely whether the row the tool cites is already
persisted by the time a tool of that turn runs.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import NotesContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import ToolMessage, UserMessage, text_content
from family_assistant.memory.index import strip_topic_index
from family_assistant.memory.invariants import MEMORY_LABEL
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS, LocalToolsProvider
from family_assistant.tools.infrastructure import TaintTrackingToolsProvider
from family_assistant.utils.clock import MockClock
from tests.functional.memory.curator_harness import (
    CURATOR_READ_POLICY,
    NOW,
    memory_db,
    review_limits,
)
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

CONVERSATION = "foreground-memory-conv"
NOTE_TITLE = "Sam"
ENTRY = "Alice said on 2026-09-17 that Sam is allergic to peanuts."
ASKED = "remember that Sam is allergic to peanuts"


def _assistant(engine: AsyncEngine, llm: RuleBasedMockLLMClient) -> ProcessingService:
    """A foreground profile that reads memory, with the real memory tool."""
    config = ProcessingServiceConfig(
        id="default_assistant",
        prompts={"system_prompt": "You are the household assistant."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=20,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.BLOCKED,
        memory_read=True,
    )
    read_policy = NoteReadPolicy.for_profile(
        visibility_grants=None, required_labels=None, memory_read=True
    )
    return ProcessingService(
        llm_client=llm,
        tools_provider=TaintTrackingToolsProvider(
            LocalToolsProvider(registrations=list(LOCAL_TOOL_REGISTRATIONS))
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
        clock=MockClock(NOW),
    )


def _before_any_tool_ran(args: MatcherArgs) -> bool:
    return not any(
        getattr(message, "role", "") == "tool" for message in args["messages"]
    )


def _proposes(message_ids: list[int] | None) -> LLMOutput:
    """The model's one tool call: an add, citing ``message_ids`` or nothing."""
    edit: dict[str, object] = {
        "op": "add",
        "note_title": NOTE_TITLE,
        "entry": ENTRY,
    }
    if message_ids is not None:
        edit["message_ids"] = message_ids
    return LLMOutput(
        content="",
        tool_calls=[
            ToolCallItem(
                id=f"call_{uuid.uuid4()}",
                type="function",
                function=ToolCallFunction(
                    name="propose_memory_edits", arguments=json.dumps({"edits": [edit]})
                ),
            )
        ],
    )


class _Transcript:
    """A fake model that proposes once, then reports what the tool replied."""

    def __init__(self, proposal: LLMOutput) -> None:
        self._proposal = proposal
        self.tool_replies: list[str] = []

    def client(self) -> RuleBasedMockLLMClient:
        rules = [
            (_before_any_tool_ran, self._proposal),
            (lambda _args: True, self._after_the_tool),
        ]
        return RuleBasedMockLLMClient(rules=rules)  # type: ignore[arg-type] # the rule tuple's own matcher and generator types

    def _after_the_tool(self, args: MatcherArgs) -> LLMOutput:
        self.tool_replies = [
            message.content
            for message in args["messages"]
            if isinstance(message, ToolMessage) and isinstance(message.content, str)
        ]
        return LLMOutput(content="Done.")


async def _say(
    service: ProcessingService, engine: AsyncEngine, said: str = ASKED
) -> None:
    result = await service.handle_chat_interaction(
        db_context=Database(engine=engine),
        chat_interface=MagicMock(),
        interface_type="web",
        conversation_id=CONVERSATION,
        trigger_content_parts=[text_content(said)],
        trigger_interface_message_id="1",
        user_name="alice",
        user_id="alice",
    )
    assert result.error_traceback is None, result.error_traceback


async def _user_row_id(db: Database) -> int:
    """The internal id of the one user row this conversation holds."""
    rows = await db.message_history.rows_matching(
        message_history_table.c.conversation_id == CONVERSATION
    )
    user_rows = [row for row in rows if row["role"] == "user"]
    assert len(user_rows) == 1, user_rows
    return int(user_rows[0]["internal_id"])


@pytest.mark.asyncio
async def test_a_foreground_edit_cites_the_message_the_person_asked_in(
    db_engine: AsyncEngine,
) -> None:
    """No ids in the call, and the entry still rests on the request's own row."""
    db = memory_db(db_engine, review_limits())
    transcript = _Transcript(_proposes(None))
    await _say(_assistant(db_engine, transcript.client()), db_engine)

    note = await db.notes.get_by_title(NOTE_TITLE, read_policy=CURATOR_READ_POLICY)
    assert note is not None, transcript.tool_replies
    assert MEMORY_LABEL in note.visibility_labels
    body = strip_topic_index(note.content)
    assert "allergic to peanuts" in body
    assert f"(refs: #{await _user_row_id(db)})" in body


@pytest.mark.asyncio
async def test_a_foreground_edit_may_not_cite_another_turn(
    db_engine: AsyncEngine,
) -> None:
    """An id the model supplies is still held to the turn it is running in."""
    db = memory_db(db_engine, review_limits())
    elsewhere = await db.message_history.add_message(
        UserMessage.from_trusted_user(content="something said in another turn"),
        interface_type="web",
        conversation_id=CONVERSATION,
        timestamp=NOW,
        turn_id="some-other-turn",
        processing_profile_id="default_assistant",
        user_id="alice",
    )

    transcript = _Transcript(_proposes([elsewhere]))
    await _say(_assistant(db_engine, transcript.client()), db_engine)

    assert (
        await db.notes.get_by_title(NOTE_TITLE, read_policy=CURATOR_READ_POLICY) is None
    )
    assert any(
        f"#{elsewhere}" in reply and "not in the current turn" in reply
        for reply in transcript.tool_replies
    ), transcript.tool_replies
