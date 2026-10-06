"""Scripts can classify text with Jev through classify() and classify_yes_no()."""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.typesafe import ChoiceQuestion, JevAnswers, NoulQuestion
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.scripting.validator import ScriptValidator
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext
from tests.functional.history_window_helpers import Recorder, make_service
from tests.mocks.fake_jev import FakeJevClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.utils.clock import MockClock


def _context(
    db_engine: AsyncEngine, mock_clock: MockClock, client: FakeJevClient | None
) -> ToolExecutionContext:
    service = make_service(mock_clock, Recorder(), budget_chars=1_000)
    service.jev_client = client
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="classify-conversation",
        user_name="test",
        turn_id=None,
        db_context=Database(engine=db_engine),
        processing_service=service,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


async def test_classify_returns_the_likeliest_label_and_every_probability(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    client = FakeJevClient(
        JevAnswers(
            "jev-test",
            {},
            {"answer": {"bills": 0.8, "school": 0.15, "other": 0.05}},
            40,
        )
    )
    engine = MontyEngine(default_timezone=ZoneInfo("UTC"))

    result = await engine.evaluate_async(
        """
classify(
    "Your electricity bill of $212 is due on Friday.",
    "What is this email about?",
    {"bills": "Invoices and payments due", "school": None, "other": None},
)
""",
        execution_context=_context(db_engine, mock_clock, client),
    )

    assert result == {
        "label": "bills",
        "probabilities": {"bills": 0.8, "school": 0.15, "other": 0.05},
    }
    state, questions = client.calls[0]
    assert state == "Your electricity bill of $212 is due on Friday."
    question = questions["answer"]
    assert isinstance(question, ChoiceQuestion)
    assert question.options == {
        "bills": "Invoices and payments due",
        "school": None,
        "other": None,
    }


async def test_classify_takes_a_plain_list_of_labels(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    client = FakeJevClient(
        JevAnswers("jev-test", {}, {"answer": {"yes": 0.3, "no": 0.7}}, 10)
    )
    engine = MontyEngine(default_timezone=ZoneInfo("UTC"))

    result = await engine.evaluate_async(
        'classify({"subject": "Re: dinner"}, "Is a reply needed?", ["yes", "no"])["label"]',
        execution_context=_context(db_engine, mock_clock, client),
    )

    assert result == "no"
    assert client.calls[0][0] == {"subject": "Re: dinner"}


async def test_classify_yes_no_returns_the_probability_of_yes(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    client = FakeJevClient(JevAnswers("jev-test", {"answer": 0.92}, {}, 10))
    engine = MontyEngine(default_timezone=ZoneInfo("UTC"))

    result = await engine.evaluate_async(
        'classify_yes_no("The garage door is open.", "Does this need action?", '
        'yes="Something should be done soon")',
        execution_context=_context(db_engine, mock_clock, client),
    )

    assert result == 0.92
    question = client.calls[0][1]["answer"]
    assert isinstance(question, NoulQuestion)
    assert question.true_criteria == "Something should be done soon"


async def test_classify_without_typesafe_says_so(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    engine = MontyEngine(default_timezone=ZoneInfo("UTC"))

    with pytest.raises(ScriptExecutionError, match="TypeSafe"):
        await engine.evaluate_async(
            'classify("text", "Which?", ["a", "b"])',
            execution_context=_context(db_engine, mock_clock, None),
        )


async def test_classify_needs_two_options(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    client = FakeJevClient(JevAnswers("jev-test", {}, {}, 0))
    engine = MontyEngine(default_timezone=ZoneInfo("UTC"))

    with pytest.raises(ScriptExecutionError, match="at least two options"):
        await engine.evaluate_async(
            'classify("text", "Which?", ["only"])',
            execution_context=_context(db_engine, mock_clock, client),
        )
    assert not client.calls


@pytest.mark.no_db
def test_scripts_using_the_classifier_type_check() -> None:
    result = ScriptValidator().validate(
        """
choice = classify("text", "Which?", ["a", "b"])
label: str = choice["label"]
p: float = classify_yes_no({"body": "text"}, "Urgent?", no="Can wait")
"""
    )

    assert result.is_valid, result.diagnostics
