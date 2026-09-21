"""A real curator profile, wired the way the shipped one is, for review tests.

Slice 5 of docs/design/conversation-memory.md. The review task runs a whole
turn on the ``memory_curator`` profile, so a test of it needs a real
``ProcessingService`` with the real tools and the real notes context provider;
only the model is a fake. What this module does *not* fake is the confinement:
the profile's read and write policies, its tool set and its memory settings are
the shipped ones, so a test that passes here is evidence about the deployment
rather than about the harness.

Source conversations are written straight through the message-history
repository, the way a turn writes them -- a user row and its terminal assistant
reply. Driving them through the chat API instead would add a second profile,
its tools and its policy to every test in order to produce rows this module can
write in three lines.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import NotesContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.memory.invariants import MEMORY_LABEL
from family_assistant.memory.limits import MemoryLimits
from family_assistant.memory.review import MEMORY_CURATOR_PROFILE_ID
from family_assistant.memory.review_settings import MemoryReviewSettings
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import Database, set_engine_memory_limits
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.security.taint import TaintMetadata

CONTRIBUTOR = "default_assistant"
"""The profile the reviewed conversation ran under."""

WEB = "web"
CONVERSATION = "review-conv"
NOW = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)
ENABLED_AT = NOW - timedelta(days=7)

SETTINGS = MemoryReviewSettings(
    idle_window_minutes={WEB: 30},
    contributing_interfaces=frozenset({WEB}),
)

CURATOR_TOOLS = frozenset({"get_note", "propose_memory_edits"})
"""Exactly what defaults.yaml grants the curator."""

PERSON_WRITE_POLICY = NoteWritePolicy(
    visibility_grants=None,
    default_labels=[MEMORY_LABEL],
    required_labels=[MEMORY_LABEL],
    allowed_labels=None,
)
"""What a person editing a memory note in the notes UI writes under."""

CURATOR_READ_POLICY = NoteReadPolicy.for_profile(
    visibility_grants=[MEMORY_LABEL],
    required_labels=[MEMORY_LABEL],
    memory_read=True,
)

REQUEST_MARKER = "## Conversation under review"


def review_limits(**overrides: int | str) -> MemoryLimits:
    """The store bounds a review test runs under, small enough to read."""
    base = {
        "core_note_max_chars": 4000,
        "topic_note_max_chars": 4000,
        "review_input_max_chars": 6000,
    }
    return MemoryLimits(**{**base, **overrides})  # type: ignore[arg-type] # the overrides are this dataclass's own fields


def memory_db(engine: AsyncEngine, limits: MemoryLimits) -> Database:
    """A handle on ``engine`` with these limits, registered engine-wide.

    Registered rather than passed, because the review's own handle comes from
    the task worker: production attaches the limits to the engine at startup
    for exactly that reason.
    """
    set_engine_memory_limits(engine, limits)
    return Database(engine=engine)


# ---------------------------------------------------------------------------
# Seeding the conversation under review
# ---------------------------------------------------------------------------


async def enable_contribution(db: Database, *, at: datetime = ENABLED_AT) -> None:
    """Record that the source profile has been contributing since ``at``."""
    await db.memory_review.record_enablement(
        profile_ids_contributing={CONTRIBUTOR}, now=at
    )


async def seed_turn(
    db: Database,
    *,
    turn_id: str,
    said: str,
    replied: str | None = "Noted.",
    speaker: str = "alice",
    at: datetime | None = None,
    conversation_id: str = CONVERSATION,
    user_taint: TaintMetadata | None = None,
    assistant_taint: TaintMetadata | None = None,
    internal: bool = False,
) -> list[int]:
    """One turn of a web conversation: what a person said and the reply.

    ``replied=None`` leaves the turn without its terminal reply, which is the
    parked or crashed turn the chunking rules are about. ``internal=True``
    writes the trigger the way the application writes its own -- a callback or
    an automation -- which is a row nobody spoke.
    """
    moment = at if at is not None else NOW - timedelta(minutes=45)
    # Both rows are stamped by default. A row with no taint metadata is not a
    # trusted row: the repository reads one back as unknown-external, on the
    # grounds that it predates runtime taint tracking. A real turn stamps
    # every row it writes, so a harness that did not would skip every stretch
    # on provenance and prove nothing.
    trusted = TurnTaintState.empty().to_metadata()
    ids = [
        await db.message_history.add_message(
            UserMessage(content=said, taint_metadata=user_taint or trusted),
            interface_type=WEB,
            conversation_id=conversation_id,
            timestamp=moment,
            turn_id=turn_id,
            processing_profile_id=CONTRIBUTOR,
            user_id=speaker,
            is_internal=internal,
        )
    ]
    if replied is not None:
        ids.append(
            await db.message_history.add_message(
                AssistantMessage(
                    content=replied, taint_metadata=assistant_taint or trusted
                ),
                interface_type=WEB,
                conversation_id=conversation_id,
                timestamp=moment + timedelta(seconds=1),
                turn_id=turn_id,
                processing_profile_id=CONTRIBUTOR,
                user_id=speaker,
            )
        )
    return ids


# ---------------------------------------------------------------------------
# The curator
# ---------------------------------------------------------------------------


def curator_service(
    engine: AsyncEngine,
    llm_client: RuleBasedMockLLMClient,
    *,
    max_iterations: int = 6,
) -> ProcessingService:
    """The shipped curator profile, with a fake model in place of the real one."""
    config = ProcessingServiceConfig(
        id=MEMORY_CURATOR_PROFILE_ID,
        prompts={
            "system_prompt": (
                "You maintain the household memory notes. Propose edits with "
                "propose_memory_edits, or reply that nothing durable happened."
            )
        },
        timezone=ZoneInfo("UTC"),
        max_history_messages=1,
        history_max_age_hours=0.5,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.BLOCKED,
        allowed_delegation_sources=[],
        visibility_grants={MEMORY_LABEL},
        required_note_read_labels=[MEMORY_LABEL],
        default_note_visibility_labels=[MEMORY_LABEL],
        required_note_visibility_labels=[MEMORY_LABEL],
        memory_read=True,
        allow_wake_llm=False,
        include_aggregated_context=True,
        max_iterations=max_iterations,
    )
    provider = LocalToolsProvider(
        registrations=[
            registration
            for registration in LOCAL_TOOL_REGISTRATIONS
            if registration.name in CURATOR_TOOLS
        ]
    )
    service = ProcessingService(
        llm_client=llm_client,
        tools_provider=PolicyEnforcingToolsProvider(
            wrapped_provider=provider,
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
            ),
        ),
        service_config=config,
        context_providers=[
            NotesContextProvider(
                get_db_context_func=lambda: Database(engine=engine),
                prompts=config.prompts,
                read_policy=CURATOR_READ_POLICY,
            )
        ],
        server_url="http://testserver",
        app_config=AppConfig(),
    )
    service.processing_services_registry = {MEMORY_CURATOR_PROFILE_ID: service}
    return service


# ---------------------------------------------------------------------------
# The fake curator model
# ---------------------------------------------------------------------------


def prompt_text(messages: Sequence[LLMMessage]) -> str:
    """Everything the model was shown, as one string.

    Joined rather than indexed: the request is not reliably the last message
    (a trailing per-turn context block follows it), and a rule that guessed at
    a position would silently stop matching when that changes.
    """
    parts: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def cited_ids(text: str) -> list[int]:
    """The message ids the rendered request offered, in the order shown."""
    return [int(match) for match in re.findall(r"#(\d+)\b", text)]


def tool_results(messages: Sequence[LLMMessage]) -> list[str]:
    """What the tools told the model, in order."""
    return [
        message.content
        for message in messages
        if isinstance(message, ToolMessage) and isinstance(message.content, str)
    ]


@dataclass
class CuratorScript:
    """A fake curator: propose once, then reply.

    Records every request it saw, which is how a test asserts on what the
    review put in front of the model -- the point of most of these tests is
    what the curator was and was not shown.
    """

    note_title: str = "Sam"
    entry: str = "Alice said on 2026-09-17 that the family always takes the tram."
    requests: list[str] = field(default_factory=list)
    propose: bool = True
    """Whether this curator proposes anything at all."""
    keep_proposing: bool = False
    """Propose again after a refusal, to exercise the retry budget."""
    cite_out_of_scope: bool = False
    """Cite a message the stretch does not contain, so the apply refuses."""

    def rules(self) -> list[tuple[object, object]]:
        """The rule list for :class:`RuleBasedMockLLMClient`."""
        return [(self._matches, self._respond)]

    def _matches(self, args: MatcherArgs) -> bool:
        return REQUEST_MARKER in prompt_text(args["messages"])

    def _respond(self, args: MatcherArgs) -> LLMOutput:
        messages = args["messages"]
        text = prompt_text(messages)
        results = tool_results(messages)
        if not results:
            self.requests.append(text)
        if not self.propose:
            return LLMOutput(content="Nothing durable happened.")
        if results and not (self.keep_proposing and self._retryable(results[-1])):
            return LLMOutput(content="Done.")
        ids = cited_ids(text)
        cited = [max(ids) + 1000] if self.cite_out_of_scope else ids[:1]
        return LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id=f"call-{len(results)}",
                    type="function",
                    function=ToolCallFunction(
                        name="propose_memory_edits",
                        arguments=json.dumps({
                            "edits": [
                                {
                                    "op": "add",
                                    "note_title": self.note_title,
                                    "entry": self.entry,
                                    "message_ids": cited,
                                }
                            ]
                        }),
                    ),
                )
            ],
        )

    @staticmethod
    def _retryable(last_result: str) -> bool:
        """Whether the tool's last word invited another proposal."""
        return "will not look at another proposal" not in last_result


def curator_llm(script: CuratorScript) -> RuleBasedMockLLMClient:
    """A mock client driven by ``script``, refusing anything it did not expect."""
    return RuleBasedMockLLMClient(
        rules=script.rules(),  # type: ignore[arg-type] # the rule tuple's own matcher and generator types
        default_response=LLMOutput(content="No memory review request was recognised."),
    )
