"""Generated one-line summaries for the conversation lists.

See docs/design/conversation-list-summaries.md. A recurring sweep finds
conversations whose visible messages have moved past their stored summary and
have since gone quiet, and summarizes each on a cheap model of its own. The
lists show the summary where there is one and the latest message otherwise.

**The summarizer reads, and nothing reads it back into a model.** It is given
no tools, and its output is shown only to the conversation's owner, in the same
place the latest-message preview it replaces already showed that conversation's
content. A summary must not be fed into a prompt: it is derived from whatever
the conversation contained, including untrusted email or web content, and
carries none of that content's taint.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.model_routing import bounded_text
from family_assistant.services.turn_resumption import TURN_RESUME_TASK_TYPE
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from family_assistant.config_models import ConversationSummaryConfig
    from family_assistant.llm import LLMInterface
    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.conversation_summaries import (
        SummaryTranscriptMessage,
    )
    from family_assistant.tools.types import ToolExecutionContext
    from family_assistant.web.conversation_stream_hub import ConversationStreamHub

logger = logging.getLogger(__name__)

CONVERSATION_SUMMARY_SWEEP_TASK_TYPE = "conversation_summary_sweep"
CONVERSATION_SUMMARY_SWEEP_TASK_ID = "system_conversation_summary_sweep"

CONVERSATION_SUMMARY_PROMPT_KEY = "conversation_summary_prompt"
"""Where the summarizer's instructions live in ``prompts.yaml``."""

MAX_SUMMARY_CHARS = 120
"""Longest summary stored. Both lists clamp to two lines; this is the backstop
for a model that ignores the length it was asked for."""

_TRANSCRIPT_HEAD = 2
_TRANSCRIPT_TAIL = 20
_CHARS_PER_MESSAGE = 600

_WHITESPACE = re.compile(r"\s+")
_WRAPPING_QUOTES = "\"'`“”‘’"
_LABEL_PREFIX = re.compile(r"^(summary|title)\s*:\s*", re.IGNORECASE)


def clean_summary(raw: str | None) -> str | None:
    """The model's answer as one bounded display line, or ``None`` if empty."""
    if not raw:
        return None
    first_line = next((line for line in raw.strip().splitlines() if line.strip()), "")
    text = _LABEL_PREFIX.sub("", _WHITESPACE.sub(" ", first_line).strip())
    text = text.strip(_WRAPPING_QUOTES).strip()
    if not text:
        return None
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    cut = text[: MAX_SUMMARY_CHARS - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


class ConversationSummarizer:
    """Turns a conversation's transcript into a one-line list label."""

    def __init__(
        self,
        llm_client: LLMInterface,
        *,
        prompt: str,
        timeout_seconds: float,
    ) -> None:
        self._llm_client = llm_client
        self._prompt = prompt
        self._timeout_seconds = timeout_seconds

    async def summarize(
        self, transcript: Sequence[SummaryTranscriptMessage]
    ) -> str | None:
        """A summary of *transcript*, or ``None`` if the model gave nothing usable.

        Raises whatever the provider raised, and ``TimeoutError``; the sweep
        decides what a failure means for the conversation.
        """
        rendered = "\n".join(
            f"{message.role}: {bounded_text(message.content.strip(), _CHARS_PER_MESSAGE)}"
            for message in transcript
            if message.content.strip()
        )
        if not rendered:
            return None
        output = await asyncio.wait_for(
            self._llm_client.generate_response(
                [
                    SystemMessage(content=self._prompt),
                    UserMessage(content=f"<conversation>\n{rendered}\n</conversation>"),
                ],
                tools=None,
            ),
            timeout=self._timeout_seconds,
        )
        return clean_summary(output.content)


async def _conversations_with_live_turns(db: Database) -> set[str]:
    """Conversations whose turn still holds a lease, on any interface.

    Every running web and Telegram turn holds one (a ``resume_interrupted_turn``
    task) until it ends, and a lease being resumed after a restart is
    ``processing``. There are only ever as many as there are turns in flight.
    """
    conversation_ids: set[str] = set()
    for status in ("pending", "processing"):
        leases = await db.tasks.get_all(
            status=status, task_type=TURN_RESUME_TASK_TYPE, limit=1000
        )
        conversation_ids.update(
            str(lease["payload"]["conversation_id"])
            for lease in leases
            if lease["payload"] and lease["payload"].get("conversation_id")
        )
    return conversation_ids


async def run_conversation_summary_sweep(
    db: Database,
    *,
    summarizer: ConversationSummarizer,
    config: ConversationSummaryConfig,
    now_fn: Callable[[], datetime] | None = None,
    on_summary_changed: Callable[[str], Awaitable[None]] | None = None,
) -> int:
    """Summarize every due conversation, up to the batch size. Returns how many
    summaries changed.

    A failed attempt still advances the conversation's watermark, keeping any
    earlier summary: retrying on every sweep would spend a model call per
    sweep on a conversation that cannot be summarized, and the next message in
    it makes it due again anyway.
    """
    now = (now_fn or SystemClock().now)()
    due = await db.conversation_summaries.select_due(
        settled_before=now - timedelta(seconds=config.idle_seconds),
        active_since=now - timedelta(days=config.lookback_days),
        limit=config.batch_size,
        exclude_conversation_ids=await _conversations_with_live_turns(db),
    )
    changed = 0
    for conversation in due:
        transcript = await db.conversation_summaries.transcript(
            conversation.conversation_id,
            through_id=conversation.latest_message_id,
            head=_TRANSCRIPT_HEAD,
            tail=_TRANSCRIPT_TAIL,
        )
        summary: str | None
        try:
            summary = await summarizer.summarize(transcript)
        except Exception:
            # Broad on purpose: one conversation the provider will not
            # summarize must not stop the rest of the batch.
            logger.exception(
                "Conversation summary failed for %s", conversation.conversation_id
            )
            summary = None
        previous = await db.conversation_summaries.get_summary(
            conversation.conversation_id
        )
        await db.conversation_summaries.record(
            conversation.conversation_id,
            summary=summary,
            through_id=conversation.latest_message_id,
            now=now,
        )
        if summary is not None and summary != previous:
            changed += 1
            if on_summary_changed is not None:
                await on_summary_changed(conversation.conversation_id)
    if due:
        logger.info(
            "Conversation summary sweep: %d due, %d summary(ies) changed.",
            len(due),
            changed,
        )
    return changed


def make_conversation_summary_sweep_handler(
    *,
    summarizer: ConversationSummarizer | None,
    config: ConversationSummaryConfig,
    stream_hub: ConversationStreamHub | None,
    # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
) -> Callable[[ToolExecutionContext, dict[str, Any]], Awaitable[None]]:
    """Bind the sweep to this process's summarizer and activity stream.

    A changed summary pings its owner's activity stream, which is what makes an
    open web or iOS list refetch and pick the summary up without a refresh.

    ``summarizer=None`` -- summaries disabled -- still yields a handler, for a
    sweep an earlier configuration seeded: it returns without doing anything.
    """

    async def handle_conversation_summary_sweep(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
        payload: dict[str, Any],
    ) -> None:
        del payload
        if summarizer is None:
            return
        db = exec_context.db_context

        async def publish(conversation_id: str) -> None:
            if stream_hub is None:
                return
            user_id = await db.conversation_summaries.latest_user_id(conversation_id)
            if user_id is None:
                return
            await stream_hub.publish_activity(
                conversation_id, user_id=user_id, reason="summary"
            )

        await run_conversation_summary_sweep(
            db,
            summarizer=summarizer,
            config=config,
            now_fn=(exec_context.clock or SystemClock()).now,
            on_summary_changed=publish,
        )

    return handle_conversation_summary_sweep
