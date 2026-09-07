"""Index message-history turns into the document search store."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from family_assistant.observability.metrics import record_indexing_documents
from family_assistant.storage.vector import (
    add_document,
    add_embedding,
    get_indexed_content_fingerprints,
    has_embeddings_outside_model,
)

if TYPE_CHECKING:
    from family_assistant.storage.database import Database, DatabaseTransaction
    from family_assistant.storage.types import MessageHistoryRow
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

MESSAGE_HISTORY_SOURCE_TYPE = "message_history"
MESSAGE_TURN_EMBEDDING_TYPE = "message_turn"
DEFAULT_MESSAGE_HISTORY_INDEX_BATCH_SIZE = 50
MAX_INDEXED_FIELD_LENGTH = 4000
MESSAGE_HISTORY_BACKFILL_TASK_ID = "system_message_history_backfill"


class MessageHistoryIndexBatchPayload(TypedDict, total=False):
    """Payload for indexing one message-history turn or a bounded backfill batch."""

    turn_id: str
    internal_id: int
    after_internal_id: int
    limit: int
    force: bool


@dataclass(frozen=True)
class MessageHistoryDocument:
    """Document protocol implementation for indexed message-history turns."""

    source_type: str
    source_id: str
    source_uri: str | None
    title: str | None
    created_at: datetime | None
    # ast-grep-ignore: no-dict-any - vector Document protocol requires source-specific metadata as dict[str, Any]
    metadata: dict[str, Any] | None
    file_path: str | None
    visibility_labels: list[str] | None
    id: int | None = None


async def enqueue_message_history_backfill_task(
    db_context: Database,
    *,
    embedding_model: str,
    limit: int = DEFAULT_MESSAGE_HISTORY_INDEX_BATCH_SIZE,
) -> None:
    """Ensure existing message history gets a semantic-search backfill.

    Seeded, not upserted. The walk carries its cursor forward in the payload of
    the continuation it enqueues for itself, so the default system-task upsert
    -- which overwrites the payload and revives a finished row -- would restart
    the whole corpus on every process start, and a deployment would pay to
    re-embed history it had already embedded.

    What decides whether the walk runs again is the corpus, not a count of how
    many times it has run: if any stored turn was embedded by a different model
    than the one configured now, that turn answers no query, and a finished
    walk is cleared so it runs again. Reading the state rather than encoding
    the model in the seed's identity is what makes a rollback work -- returning
    to a model the corpus has since been re-embedded away from needs the same
    repair as moving to a new one, and an id keyed on the model would call it
    already done.

    An occurrence that is still pending or running is never disturbed, so a
    restart mid-migration resumes rather than rewinding.
    """
    if await has_embeddings_outside_model(
        db_context,
        embedding_type=MESSAGE_TURN_EMBEDDING_TYPE,
        embedding_model=embedding_model,
    ) and await db_context.tasks.delete_finished(MESSAGE_HISTORY_BACKFILL_TASK_ID):
        logger.info(
            "Message history holds turns embedded by a model other than "
            "%s; queueing the backfill to re-index them.",
            embedding_model,
        )

    await db_context.tasks.enqueue(
        task_id=MESSAGE_HISTORY_BACKFILL_TASK_ID,
        task_type="index_message_history_batch",
        payload={"limit": limit},
        max_retries_override=5,
        only_if_absent=True,
        scheduled_at=_background_index_due_now(),
    )


def _background_index_due_now() -> datetime:
    """Due immediately, but *scheduled* -- which is not the same thing here.

    Dequeue orders by ``scheduled_at`` with nulls first, so an unscheduled task
    outranks every scheduled one however long that one has been due. A backfill
    enqueues its own continuation from inside its predecessor's handler, so the
    chain keeps a null-scheduled row in front of the queue for the whole walk:
    in one incident that held every scheduled task -- delegation polls, the
    hourly cleanup -- for 96 minutes, with no backlog to see, because the chain
    regenerates exactly one row at exactly the moment it matters.

    Carrying a timestamp is the whole fix; delaying is not. The walk is a
    chain, so any per-batch delay multiplies -- thirty seconds across a corpus
    that re-embeds in four thousand batches is a day and a half. A task due now
    runs at full speed and simply takes its turn by due time, which is what
    lets a poll that has been due for forty minutes go first.

    This is a stopgap for the enqueue sites this change touches. Ordering the
    queue by due time and giving it priority lanes is
    docs/design/task-queue-priority-lanes.md, which retires the fairness role
    of these timestamps and of the per-row delay.
    """
    return datetime.now(UTC)


@dataclass(frozen=True)
class _TurnIndexCandidate:
    """One turn resolved to everything the index needs, before the model call."""

    source_id: str
    text: str
    content_hash: str
    document: MessageHistoryDocument
    start_timestamp: str
    end_timestamp: str


def _build_turn_index_candidate(
    db_context: Database,
    rows: list[MessageHistoryRow],
) -> _TurnIndexCandidate | None:
    """Resolve one group of rows into an index candidate, or None if it is empty."""
    text = build_message_turn_index_text(rows)
    if not text.strip():
        return None

    first_row = rows[0]
    last_row = rows[-1]
    source_id = db_context.message_history.get_index_source_id(first_row)
    return _TurnIndexCandidate(
        source_id=source_id,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        document=MessageHistoryDocument(
            source_type=MESSAGE_HISTORY_SOURCE_TYPE,
            source_id=source_id,
            source_uri=None,
            title=_build_turn_title(first_row),
            created_at=first_row["timestamp"],
            metadata=_build_document_metadata(rows),
            file_path=None,
            visibility_labels=[],
        ),
        start_timestamp=first_row["timestamp"].isoformat(),
        end_timestamp=last_row["timestamp"].isoformat(),
    )


async def _select_candidates_needing_embedding(
    db_context: Database,
    candidates: list[_TurnIndexCandidate],
    embedding_model: str,
    *,
    force: bool = False,
) -> list[_TurnIndexCandidate]:
    """Drop candidates whose stored embedding already covers this text and model.

    Every path into message-history indexing funnels through here -- a
    restarted backfill, the several per-row tasks one turn enqueues, a task
    retry -- so a redundant request costs this read rather than a billed
    provider call. The write side upserts on ``source_id``, so those redundant
    passes never produced duplicate rows; they produced duplicate spend.

    The read is not a claim on the turn. Workers that run two of a turn's
    sibling tasks close enough together both see no fingerprint and both
    embed, which bounds the waste at the number of workers rather than
    eliminating it -- see the design doc's deliberate simplifications.

    ``force`` is the operator's way past the check, for the changes it cannot
    see: the fingerprint compares the model's name, so a rebuild after the
    indexed text changes, or after the embedding space moves under a name that
    stayed the same, has to be asked for.
    """
    fingerprints = (
        {}
        if force
        else await get_indexed_content_fingerprints(
            db_context,
            source_ids=[candidate.source_id for candidate in candidates],
            embedding_type=MESSAGE_TURN_EMBEDDING_TYPE,
        )
    )
    stale: list[_TurnIndexCandidate] = []
    for candidate in candidates:
        indexed = fingerprints.get(candidate.source_id)
        if (
            indexed is not None
            and indexed.content_hash == candidate.content_hash
            and indexed.embedding_model == embedding_model
        ):
            continue
        stale.append(candidate)

    record_indexing_documents(
        source_type=MESSAGE_HISTORY_SOURCE_TYPE,
        outcome="skipped_unchanged",
        count=len(candidates) - len(stale),
    )
    return stale


async def _index_message_turn_with_embedding(
    db_context: Database,
    candidate: _TurnIndexCandidate,
    embedding: list[float],
    embedding_model: str,
) -> None:
    """Add document and embedding atomically.

    Document and embedding must be committed together or the document is
    permanently unsearchable.
    """

    async def _index(txn: DatabaseTransaction) -> None:
        """Add document and embedding as one unit."""
        doc_id = await add_document(txn, candidate.document)
        await add_embedding(
            db_context=txn,
            document_id=doc_id,
            chunk_index=0,
            embedding_type=MESSAGE_TURN_EMBEDDING_TYPE,
            embedding=embedding,
            embedding_model=embedding_model,
            content=candidate.text,
            content_hash=candidate.content_hash,
            embedding_doc_metadata={
                "source_id": candidate.source_id,
                "start_timestamp": candidate.start_timestamp,
                "end_timestamp": candidate.end_timestamp,
            },
        )

    await db_context.atomic(_index)
    record_indexing_documents(
        source_type=MESSAGE_HISTORY_SOURCE_TYPE,
        outcome="embedded",
        count=1,
    )


async def handle_index_message_history_batch(
    exec_context: ToolExecutionContext,
    payload: MessageHistoryIndexBatchPayload,
) -> None:
    """Index a bounded batch of message-history turns into the vector store."""
    db_context = exec_context.db_context
    embedding_generator = exec_context.embedding_generator
    if embedding_generator is None:
        raise ValueError("Message history indexing requires an embedding generator.")

    limit = payload.get("limit", DEFAULT_MESSAGE_HISTORY_INDEX_BATCH_SIZE)
    (
        groups,
        next_after_internal_id,
    ) = await db_context.message_history.get_indexable_message_groups(
        turn_id=payload.get("turn_id"),
        internal_id=payload.get("internal_id"),
        after_internal_id=payload.get("after_internal_id"),
        limit=limit,
    )
    if not groups:
        return

    candidates = [
        candidate
        for rows in groups
        if (candidate := _build_turn_index_candidate(db_context, rows)) is not None
    ]
    for candidate in await _select_candidates_needing_embedding(
        db_context,
        candidates,
        embedding_generator.model_name,
        force=payload.get("force", False),
    ):
        # Generate embeddings BEFORE the transaction block — this is a network
        # call and must not hold a database transaction.
        embedding_result = await embedding_generator.generate_embeddings([
            candidate.text
        ])
        if not embedding_result.embeddings:
            raise ValueError(
                f"Embedding generator returned no vector for {candidate.source_id}."
            )
        if embedding_result.model_name != embedding_generator.model_name:
            # The skip above compares the stored model against the generator's
            # name, and the row below is written with the result's. They are
            # the same thing by the EmbeddingGenerator contract; if they ever
            # part, no fingerprint would match and the whole corpus would
            # re-embed on every pass -- silently, and for good.
            raise ValueError(
                f"Embedding generator {embedding_generator.model_name!r} "
                f"returned a result for model {embedding_result.model_name!r}. "
                "The re-index check cannot tell whether stored embeddings are "
                "current when those disagree."
            )

        # Add document and embedding atomically
        await _index_message_turn_with_embedding(
            db_context,
            candidate,
            embedding_result.embeddings[0],
            embedding_result.model_name,
        )

    if "turn_id" not in payload and "internal_id" not in payload:
        await db_context.tasks.enqueue(
            task_id=f"index_message_history_batch_{uuid.uuid4()}",
            task_type="index_message_history_batch",
            payload={
                "after_internal_id": next_after_internal_id,
                "limit": limit,
                "force": payload.get("force", False),
            },
            max_retries_override=5,
            scheduled_at=_background_index_due_now(),
        )


def build_message_turn_index_text(rows: list[MessageHistoryRow]) -> str:
    """Build compact text for one indexed message-history turn."""
    lines: list[str] = []
    for row in rows:
        role = row["role"].capitalize()
        content = _truncate(row.get("content") or "")
        if content:
            if row["role"] == "tool" and row.get("tool_name"):
                lines.append(f"Tool {row['tool_name']} result: {content}")
            else:
                lines.append(f"{role}: {content}")

        for tool_call in row.get("tool_calls") or []:
            function = getattr(tool_call, "function", None)
            if function is None:
                continue
            arguments = _truncate(json.dumps(function.arguments, default=str))
            lines.append(f"Tool {function.name} args: {arguments}")

        attachments = row.get("attachments") or []
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                filename = attachment.get("filename") or attachment.get("name")
                description = attachment.get("description")
                if filename or description:
                    lines.append(
                        f"Attachment: {_truncate(str(filename or description))}"
                    )

    return "\n".join(lines)


def _build_turn_title(row: MessageHistoryRow) -> str:
    turn_id = row.get("turn_id")
    if turn_id:
        return f"Message history turn {turn_id}"
    return f"Message history row {row['internal_id']}"


# ast-grep-ignore: no-dict-any - vector Document protocol requires source-specific metadata as dict[str, Any]
def _build_document_metadata(rows: list[MessageHistoryRow]) -> dict[str, Any]:
    first_row = rows[0]
    last_row = rows[-1]
    roles = sorted({row["role"] for row in rows})
    tool_names = sorted(
        tool_name for row in rows if isinstance(tool_name := row.get("tool_name"), str)
    )
    return {
        "message_ids": [row["internal_id"] for row in rows],
        "conversation_id": first_row["conversation_id"],
        "interface_type": first_row["interface_type"],
        "user_id": first_row["user_id"],
        "turn_id": first_row["turn_id"],
        "thread_root_id": first_row["thread_root_id"],
        "processing_profile_id": first_row["processing_profile_id"],
        "subconversation_id": first_row["subconversation_id"],
        "start_timestamp": first_row["timestamp"].isoformat(),
        "end_timestamp": last_row["timestamp"].isoformat(),
        "roles": roles,
        "tool_names": tool_names,
    }


def _truncate(value: str) -> str:
    if len(value) <= MAX_INDEXED_FIELD_LENGTH:
        return value
    return f"{value[:MAX_INDEXED_FIELD_LENGTH]}..."
