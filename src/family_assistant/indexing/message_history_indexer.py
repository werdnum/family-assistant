"""Index message-history turns into the document search store."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from family_assistant.storage.vector import add_document, add_embedding

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.storage.types import MessageHistoryRow
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

MESSAGE_HISTORY_SOURCE_TYPE = "message_history"
MESSAGE_TURN_EMBEDDING_TYPE = "message_turn"
DEFAULT_MESSAGE_HISTORY_INDEX_BATCH_SIZE = 50
MAX_INDEXED_FIELD_LENGTH = 4000


class MessageHistoryIndexBatchPayload(TypedDict, total=False):
    """Payload for indexing one message-history turn or a bounded backfill batch."""

    turn_id: str
    internal_id: int
    after_internal_id: int
    limit: int


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

    for rows in groups:
        text = build_message_turn_index_text(rows)
        if not text.strip():
            continue

        first_row = rows[0]
        last_row = rows[-1]
        source_id = db_context.message_history.get_index_source_id(first_row)
        document = MessageHistoryDocument(
            source_type=MESSAGE_HISTORY_SOURCE_TYPE,
            source_id=source_id,
            source_uri=None,
            title=_build_turn_title(first_row),
            created_at=first_row["timestamp"],
            metadata=_build_document_metadata(rows),
            file_path=None,
            visibility_labels=[],
        )
        document_id = await add_document(db_context, document)
        embedding_result = await embedding_generator.generate_embeddings([text])
        if not embedding_result.embeddings:
            raise ValueError(f"Embedding generator returned no vector for {source_id}.")

        await add_embedding(
            db_context=db_context,
            document_id=document_id,
            chunk_index=0,
            embedding_type=MESSAGE_TURN_EMBEDDING_TYPE,
            embedding=embedding_result.embeddings[0],
            embedding_model=embedding_result.model_name,
            content=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            embedding_doc_metadata={
                "source_id": source_id,
                "start_timestamp": first_row["timestamp"].isoformat(),
                "end_timestamp": last_row["timestamp"].isoformat(),
            },
        )

    if "turn_id" not in payload and "internal_id" not in payload:
        await db_context.tasks.enqueue(
            task_id=f"index_message_history_batch_{uuid.uuid4()}",
            task_type="index_message_history_batch",
            payload={
                "after_internal_id": next_after_internal_id,
                "limit": limit,
            },
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
