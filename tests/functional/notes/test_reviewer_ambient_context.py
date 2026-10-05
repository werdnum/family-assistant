"""Eligible ambient material reaches the tool-call reviewer, and nothing else.

Milestone 5 of docs/design/ambient-note-admission-at-write-time.md: the
turn-context block the reviewer skips also carries unreviewed titles, so the
reviewed notes and skills reach it through their own bounded section, rendered
by the prompt's own renderer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolCallReviewConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyMode,
)
from family_assistant.services.tool_call_review import (
    ToolCallReviewer,
    ToolCallReviewResponse,
    ToolCallReviewVerdict,
)
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.notes import add_or_update_note_tool
from tests.functional.notes.ambient_helpers import (
    SKILL_BODY,
    notes_provider,
    state_at,
    tool_context,
    tracker_at,
    write_note,
)
from tests.mocks.mock_llm import (
    RuleBasedMockLLMClient,
    extract_text_from_content,
    get_message_content,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


async def _seed(db: Database) -> None:
    await write_note(
        db,
        "Packing procedure",
        "Roll clothes.",
        provenance=NoteProvenanceStamp.admitted(
            title="Packing procedure", decided_by="test"
        ),
    )
    await write_note(
        db,
        "Packing skill",
        SKILL_BODY,
        include_in_prompt=False,
        provenance=NoteProvenanceStamp.admitted(
            title="Packing skill", decided_by="test"
        ),
    )
    await write_note(
        db,
        "Web digest",
        "UNREVIEWED BODY",
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
    )
    await write_note(
        db,
        "Unreviewed reference title",
        "x",
        include_in_prompt=False,
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
    )


async def _what_the_reviewer_read(db: Database) -> str:
    """Review an ambient note write in an external turn; return the reviewer's prompt.

    The reviewer denies, so the write is refused and the notes the prompt
    renders are the same before and after the review.
    """
    reviewer_llm = RuleBasedMockLLMClient(
        rules=[],
        structured_rules=[
            (
                lambda _args: True,
                ToolCallReviewResponse(
                    verdict=ToolCallReviewVerdict.DENY, reason="Scripted deny."
                ),
            )
        ],
    )
    review_config = ToolCallReviewConfig()
    gate = TaintTrackingToolsProvider(
        LocalToolsProvider(registrations=[]),
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
        tool_call_reviewer=ToolCallReviewer(reviewer_llm, review_config),
        review_config=review_config,
    )
    context = tool_context(db, tracker_at(SourceTrustTier.UNKNOWN_EXTERNAL))
    context.tools_provider = gate
    context.tool_call_review_messages = []
    context.processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="unused")
        ),
        tools_provider=gate,
        service_config=ProcessingServiceConfig(
            id="ambient-review-profile",
            prompts={},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=0,
            history_min_turns=0,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        context_providers=[notes_provider(db)],
        server_url=None,
        app_config=AppConfig(),
    )

    await add_or_update_note_tool(
        context,
        title="Trip checklist",
        content="Bring the passport.",
        include_in_prompt=True,
    )

    review_calls = reviewer_llm.get_calls()
    assert len(review_calls) == 1
    return "\n".join(
        extract_text_from_content(get_message_content(message))
        for message in review_calls[0]["kwargs"]["messages"]
    )


@pytest.mark.asyncio
async def test_the_reviewer_reads_reviewed_material_as_the_prompt_renders_it(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _seed(db)
    prompt_fragments = await notes_provider(db).get_context_fragments(
        acting_user_id=None
    )
    reviewed_fragments = [
        next(fragment for fragment in prompt_fragments if marker in fragment)
        for marker in ("Roll clothes.", "Pack for a trip")
    ]

    reviewer_prompt = await _what_the_reviewer_read(db)

    for fragment in reviewed_fragments:
        assert fragment in reviewer_prompt


@pytest.mark.asyncio
async def test_unreviewed_titles_never_reach_the_reviewer(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _seed(db)

    reviewer_prompt = await _what_the_reviewer_read(db)

    assert "Roll clothes." in reviewer_prompt
    assert "Web digest" not in reviewer_prompt
    assert "UNREVIEWED BODY" not in reviewer_prompt
    assert "Unreviewed reference title" not in reviewer_prompt
