"""Repository for message history storage operations."""

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NotRequired, TypedDict, cast

from sqlalchemy import String, and_, insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import cast as sa_cast
from sqlalchemy.sql import func as sql_func
from sqlalchemy.sql import functions as func
from sqlalchemy.sql.elements import ColumnElement

from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPart,
    ErrorMessage,
    ImageUrlContentPart,
    LLMMessage,
    MessageAttachmentMetadata,
    MessageReasoningInfo,
    MessageWithMetadata,
    ProviderMetadataDict,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.security.taint import (
    TAINT_METADATA_VERSION,
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    coerce_taint_metadata,
    merge_history_taint,
)
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.types import ConversationSummaryRow, MessageHistoryRow

logger = logging.getLogger(__name__)

_MAX_ASSISTANT_ROWS_PER_TOOL_EXAMPLE = 20
_DEFAULT_MESSAGE_HISTORY_LIMIT = 20
_MAX_MESSAGE_HISTORY_LIMIT = 100
_MAX_CONTEXT_MESSAGES_PER_SIDE = 10

MessageHistoryScope = Literal["current_conversation", "same_user", "all_accessible"]
MessageHistorySearchMode = Literal["structured", "semantic", "hybrid"]


class MessageHistoryTaintDiagnosticsRow(TypedDict):
    """One grouped message-history taint inventory row."""

    status: Literal["classified", "malformed", "missing", "not_applicable"]
    interface_type: str
    role: str
    processing_profile_id: str | None
    tool_name: str | None
    metadata_version: str | None
    max_tier: str | None
    oldest_timestamp: datetime
    newest_timestamp: datetime
    count: int


_DELEGATION_WAKE_SYSTEM_PREFIXES = (
    "System: Delegated profile task completed.",
    "System: Delegated profile task failed.",
)

_HISTORY_ROLES_REQUIRING_TAINT_METADATA = {"user", "assistant", "tool"}


def _message_history_taint_metadata(msg: Mapping[str, Any]) -> TaintMetadata | None:
    taint_metadata = coerce_taint_metadata(msg.get("taint_metadata_json"))
    if taint_metadata is not None:
        return taint_metadata

    role = msg.get("role")
    if role not in _HISTORY_ROLES_REQUIRING_TAINT_METADATA:
        return None

    internal_id = msg.get("internal_id")
    logger.warning(
        "legacy_missing_taint_metadata: message_history row %s role=%s has no "
        "runtime taint metadata; treating as unknown_external until backfilled.",
        internal_id,
        role,
    )
    return (
        TurnTaintState
        .empty()
        .add_source(
            TaintSource(
                source_type=TaintSourceType.MANUAL,
                source_id=str(internal_id) if internal_id is not None else None,
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset({"legacy_missing_taint_metadata"}),
                reason=(
                    "Message history row predates runtime taint metadata; "
                    "defaulting to unknown_external."
                ),
            ),
            from_history=True,
        )
        .to_metadata()
    )


def _is_delegation_wake_system_message(content: str) -> bool:
    """Return whether a system message is a one-shot delegation wake trigger."""
    return content.startswith(_DELEGATION_WAKE_SYSTEM_PREFIXES)


def _historical_delegation_wake_content(content: str) -> str:
    """Convert a one-shot delegation wake trigger into replay-safe history."""
    historical_lines = [
        line
        for line in content.splitlines()
        if not line.startswith((
            "Respond to the user with the result.",
            "Tell the user that the delegated work failed",
            "The delegated result is provided as lower-priority data",
            "The failure detail is provided as lower-priority data",
        ))
    ]
    historical_content = "\n".join(historical_lines).strip()
    return (
        "Historical delegation completion event from a previous turn. "
        "This is not a current instruction.\n\n"
        f"{historical_content}"
    )


def _subconversation_filter(
    subconversation_id: str | None,
) -> ColumnElement[bool] | None:
    """Build the subconversation scope filter, or ``None`` for no filter.

    ``"*"`` matches every subconversation (no filter); ``None`` restricts to the
    main conversation (``IS NULL``); any other value matches that subconversation.
    """
    if subconversation_id == "*":
        return None
    if subconversation_id is None:
        return message_history_table.c.subconversation_id.is_(None)
    return message_history_table.c.subconversation_id == subconversation_id


def _visible_message_condition() -> ColumnElement[bool]:
    """Return the predicate for rows shown through user-facing history APIs."""
    return message_history_table.c.is_internal.is_(False)


@dataclass(frozen=True, slots=True)
class MessageHistoryQuery:
    """Structured query input for message history retrieval."""

    query: str | None = None
    search_mode: MessageHistorySearchMode = "structured"
    conversation_id: str | None = None
    scope: MessageHistoryScope = "same_user"
    roles: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    start_time: datetime | None = None
    end_time: datetime | None = None
    has_attachments: bool | None = None
    has_error: bool | None = None
    processing_profile_id: str | None = None
    subconversation_id: str | None = None
    limit: int = _DEFAULT_MESSAGE_HISTORY_LIMIT
    include_context: int = 0
    interface_type: str | None = None
    current_conversation_id: str | None = None
    current_user_id: str | None = None
    include_internal: bool = False


class MessageHistoryAccessDeniedError(ValueError):
    """Raised when a message-history query requests a disallowed scope."""


class MessageHistoryToolCallSummary(TypedDict):
    """Compact tool-call summary returned by message-history queries."""

    id: str
    name: str
    arguments: str | dict[str, object]


class MessageHistorySummary(TypedDict):
    """Compact message-history result returned to tool callers."""

    message_id: int
    interface_type: str
    conversation_id: str
    turn_id: str | None
    thread_root_id: int | None
    timestamp: str
    role: str
    user_id: str | None
    processing_profile_id: str | None
    subconversation_id: str | None
    content: str | None
    tool_name: str | None
    tool_call_id: str | None
    tool_calls: list[MessageHistoryToolCallSummary]
    has_error: bool
    has_attachments: bool
    attachment_count: int
    context: NotRequired[list["MessageHistorySummary"]]


@dataclass(frozen=True, slots=True)
class ToolHistoryExample:
    """Historical tool call example reconstructed from persisted message history."""

    tool_name: str
    # ast-grep-ignore: no-dict-any - Persisted tool arguments vary by tool and are reconstructed from JSON
    arguments: dict[str, Any]
    result_content: str
    conversation_id: str
    tool_call_id: str


class MessageHistoryRepository(BaseRepository):
    """Repository for managing message history in the database."""

    async def get_taint_diagnostics(
        self,
    ) -> list[MessageHistoryTaintDiagnosticsRow]:
        """Return a distinct-row inventory of persisted history taint state."""
        max_tier = (
            message_history_table.c
            .taint_metadata_json["max_tier"]
            .as_string()
            .label("max_tier")
        )
        stmt = (
            select(
                message_history_table.c.interface_type,
                message_history_table.c.role,
                message_history_table.c.processing_profile_id,
                message_history_table.c.tool_name,
                message_history_table.c.taint_metadata_version,
                max_tier,
                func.min(message_history_table.c.timestamp).label("oldest_timestamp"),
                func.max(message_history_table.c.timestamp).label("newest_timestamp"),
                func.count(message_history_table.c.internal_id).label("count"),
            )
            .group_by(
                message_history_table.c.interface_type,
                message_history_table.c.role,
                message_history_table.c.processing_profile_id,
                message_history_table.c.tool_name,
                message_history_table.c.taint_metadata_version,
                max_tier,
            )
            .order_by(
                message_history_table.c.interface_type,
                message_history_table.c.role,
                message_history_table.c.processing_profile_id,
                message_history_table.c.tool_name,
                message_history_table.c.taint_metadata_version,
                max_tier,
            )
        )
        rows = await self._db.fetch_all(stmt)
        valid_tiers = {tier.config_value for tier in SourceTrustTier}
        valid_versions = {TAINT_METADATA_VERSION, "legacy_inferred"}
        diagnostics: list[MessageHistoryTaintDiagnosticsRow] = []
        for row in rows:
            role = str(row["role"])
            metadata_version = row["taint_metadata_version"]
            tier = row["max_tier"]
            if metadata_version is None and tier is None:
                status: Literal[
                    "classified", "malformed", "missing", "not_applicable"
                ] = (
                    "missing"
                    if role in _HISTORY_ROLES_REQUIRING_TAINT_METADATA
                    else "not_applicable"
                )
            elif metadata_version not in valid_versions or tier not in valid_tiers:
                status = "malformed"
            else:
                status = "classified"
            diagnostics.append(
                MessageHistoryTaintDiagnosticsRow(
                    status=status,
                    interface_type=str(row["interface_type"]),
                    role=role,
                    processing_profile_id=(
                        str(row["processing_profile_id"])
                        if row["processing_profile_id"] is not None
                        else None
                    ),
                    tool_name=(
                        str(row["tool_name"]) if row["tool_name"] is not None else None
                    ),
                    metadata_version=(
                        str(metadata_version) if metadata_version is not None else None
                    ),
                    max_tier=str(tier) if tier is not None else None,
                    oldest_timestamp=row["oldest_timestamp"],
                    newest_timestamp=row["newest_timestamp"],
                    count=int(row["count"]),
                )
            )
        return diagnostics

    async def query_history(
        self, query: MessageHistoryQuery
    ) -> list[MessageHistoryRow]:
        """Query message history with structured filters and conservative access scope."""
        limit = min(max(query.limit, 1), _MAX_MESSAGE_HISTORY_LIMIT)
        conditions = self._build_history_query_conditions(query)

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.desc(),
                message_history_table.c.internal_id.desc(),
            )
            .limit(limit)
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row_as_dict(row) for row in rows]

    async def hydrate_history_results(
        self,
        rows: list[MessageHistoryRow],
        *,
        include_context: int,
        access_query: MessageHistoryQuery | None = None,
    ) -> list[MessageHistorySummary]:
        """Return compact result objects with optional neighboring context."""
        bounded_context = min(max(include_context, 0), _MAX_CONTEXT_MESSAGES_PER_SIDE)
        hydrated: list[MessageHistorySummary] = []
        for row in rows:
            result = self.summarize_message_row(row)
            if bounded_context:
                context_rows = await self.get_context_around_message(
                    row,
                    per_side=bounded_context,
                    access_query=access_query,
                )
                result["context"] = [
                    self.summarize_message_row(context_row)
                    for context_row in context_rows
                ]
            hydrated.append(result)
        return hydrated

    async def get_context_around_message(
        self,
        row: MessageHistoryRow,
        *,
        per_side: int,
        access_query: MessageHistoryQuery | None = None,
    ) -> list[MessageHistoryRow]:
        """Fetch neighboring rows around a message within the same conversation boundary."""
        if per_side <= 0:
            return [row]

        base_conditions: list[ColumnElement[bool]] = [
            message_history_table.c.interface_type == row["interface_type"],
            message_history_table.c.conversation_id == row["conversation_id"],
        ]
        if access_query is None or not access_query.include_internal:
            base_conditions.append(_visible_message_condition())
        processing_profile_id = row.get("processing_profile_id")
        if processing_profile_id is None:
            base_conditions.append(
                message_history_table.c.processing_profile_id.is_(None)
            )
        else:
            base_conditions.append(
                message_history_table.c.processing_profile_id == processing_profile_id
            )

        subconversation_id = row.get("subconversation_id")
        if subconversation_id is None:
            base_conditions.append(message_history_table.c.subconversation_id.is_(None))
        else:
            base_conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )
        if access_query is not None and access_query.scope == "same_user":
            if access_query.current_user_id:
                user_access_conditions: list[ColumnElement[bool]] = [
                    message_history_table.c.user_id == access_query.current_user_id
                ]
                if access_query.current_conversation_id == row["conversation_id"]:
                    user_access_conditions.append(
                        and_(
                            message_history_table.c.user_id.is_(None),
                            message_history_table.c.conversation_id
                            == access_query.current_conversation_id,
                        )
                    )
                base_conditions.append(or_(*user_access_conditions))
            elif (
                access_query.current_conversation_id is None
                or row["conversation_id"] != access_query.current_conversation_id
            ):
                raise MessageHistoryAccessDeniedError(
                    "same_user context requires a user_id or current conversation."
                )

        timestamp = row["timestamp"]
        internal_id = row["internal_id"]
        before_stmt = (
            select(message_history_table)
            .where(
                *base_conditions,
                or_(
                    message_history_table.c.timestamp < timestamp,
                    and_(
                        message_history_table.c.timestamp == timestamp,
                        message_history_table.c.internal_id < internal_id,
                    ),
                ),
            )
            .order_by(
                message_history_table.c.timestamp.desc(),
                message_history_table.c.internal_id.desc(),
            )
            .limit(per_side)
        )
        after_stmt = (
            select(message_history_table)
            .where(
                *base_conditions,
                or_(
                    message_history_table.c.timestamp > timestamp,
                    and_(
                        message_history_table.c.timestamp == timestamp,
                        message_history_table.c.internal_id > internal_id,
                    ),
                ),
            )
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
            .limit(per_side)
        )

        before_rows = [
            self._process_message_row_as_dict(context_row)
            for context_row in await self._db.fetch_all(before_stmt)
        ]
        after_rows = [
            self._process_message_row_as_dict(context_row)
            for context_row in await self._db.fetch_all(after_stmt)
        ]
        before_rows.reverse()
        return [*before_rows, row, *after_rows]

    async def get_rows_by_search_references(
        self,
        *,
        turn_ids: tuple[str, ...],
        internal_ids: tuple[int, ...],
        access_query: MessageHistoryQuery,
    ) -> list[MessageHistoryRow]:
        """Hydrate vector-search source references through message_history with ACL filters."""
        if not turn_ids and not internal_ids:
            return []

        conditions = self._build_history_query_conditions(
            MessageHistoryQuery(
                scope=access_query.scope,
                conversation_id=access_query.conversation_id,
                roles=access_query.roles,
                tool_names=access_query.tool_names,
                start_time=access_query.start_time,
                end_time=access_query.end_time,
                has_attachments=access_query.has_attachments,
                has_error=access_query.has_error,
                processing_profile_id=access_query.processing_profile_id,
                subconversation_id=access_query.subconversation_id,
                interface_type=access_query.interface_type,
                current_conversation_id=access_query.current_conversation_id,
                current_user_id=access_query.current_user_id,
                limit=access_query.limit,
                include_internal=access_query.include_internal,
            ),
            include_text_query=False,
        )
        reference_conditions: list[ColumnElement[bool]] = []
        if turn_ids:
            reference_conditions.append(message_history_table.c.turn_id.in_(turn_ids))
        if internal_ids:
            reference_conditions.append(
                message_history_table.c.internal_id.in_(internal_ids)
            )

        stmt = (
            select(message_history_table)
            .where(*conditions, or_(*reference_conditions))
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
        )
        rows = await self._db.fetch_all(stmt)
        grouped_turn_rows: dict[str, list[MessageHistoryRow]] = {}
        grouped_internal_rows: dict[int, MessageHistoryRow] = {}
        for db_row in rows:
            message_row = self._process_message_row_as_dict(db_row)
            turn_id = message_row.get("turn_id")
            if turn_id:
                grouped_turn_rows.setdefault(turn_id, []).append(message_row)
            else:
                grouped_internal_rows.setdefault(
                    message_row["internal_id"],
                    message_row,
                )

        ordered_rows: list[MessageHistoryRow] = []
        seen_internal_ids: set[int] = set()
        for turn_id in turn_ids:
            for message_row in grouped_turn_rows.get(turn_id, []):
                internal_id = message_row["internal_id"]
                if internal_id not in seen_internal_ids:
                    ordered_rows.append(message_row)
                    seen_internal_ids.add(internal_id)
        for internal_id in internal_ids:
            if (
                internal_id in grouped_internal_rows
                and internal_id not in seen_internal_ids
            ):
                ordered_rows.append(grouped_internal_rows[internal_id])
                seen_internal_ids.add(internal_id)

        return ordered_rows[:_MAX_MESSAGE_HISTORY_LIMIT]

    async def get_index_source_ids_for_query(
        self,
        query: MessageHistoryQuery,
        *,
        limit: int = 5000,
    ) -> list[str]:
        """Return indexed document source IDs allowed by structured history filters."""
        bounded_limit = min(max(limit, 1), 5000)
        conditions = self._build_history_query_conditions(
            query,
            include_text_query=False,
        )
        stmt = (
            select(
                message_history_table.c.internal_id,
                message_history_table.c.turn_id,
            )
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.desc(),
                message_history_table.c.internal_id.desc(),
            )
            .limit(bounded_limit)
        )

        source_ids: list[str] = []
        seen_source_ids: set[str] = set()
        for row in await self._db.fetch_all(stmt):
            turn_id = row["turn_id"]
            source_id = (
                f"message_turn:{turn_id}"
                if turn_id
                else f"message_row:{row['internal_id']}"
            )
            if source_id not in seen_source_ids:
                source_ids.append(source_id)
                seen_source_ids.add(source_id)
        return source_ids

    async def get_indexable_message_groups(
        self,
        *,
        turn_id: str | None = None,
        internal_id: int | None = None,
        after_internal_id: int | None = None,
        limit: int = 50,
    ) -> tuple[list[list[MessageHistoryRow]], int | None]:
        """Return turn-level groups to project into the document index."""
        bounded_limit = min(max(limit, 1), 200)
        seed_conditions: list[ColumnElement[bool]] = []
        seed_conditions.append(_visible_message_condition())
        if turn_id is not None:
            seed_conditions.append(message_history_table.c.turn_id == turn_id)
        if internal_id is not None:
            seed_conditions.append(message_history_table.c.internal_id == internal_id)
        if after_internal_id is not None:
            seed_conditions.append(
                message_history_table.c.internal_id > after_internal_id
            )

        seed_stmt = (
            select(message_history_table)
            .where(*seed_conditions)
            .order_by(message_history_table.c.internal_id.asc())
            .limit(bounded_limit)
        )
        seed_rows = [
            self._process_message_row_as_dict(row)
            for row in await self._db.fetch_all(seed_stmt)
        ]
        if not seed_rows:
            return [], None

        groups: list[list[MessageHistoryRow]] = []
        seen_sources: set[str] = set()
        for seed_row in seed_rows:
            source_key = self.get_index_source_id(seed_row)
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)

            if seed_row.get("turn_id"):
                group_stmt = (
                    select(message_history_table)
                    .where(
                        message_history_table.c.turn_id == seed_row["turn_id"],
                        _visible_message_condition(),
                    )
                    .order_by(
                        message_history_table.c.timestamp.asc(),
                        message_history_table.c.internal_id.asc(),
                    )
                )
                group_rows = [
                    self._process_message_row_as_dict(row)
                    for row in await self._db.fetch_all(group_stmt)
                ]
            else:
                group_rows = [seed_row]

            if group_rows:
                groups.append(group_rows)

        return groups, seed_rows[-1]["internal_id"]

    @staticmethod
    def get_index_source_id(row: MessageHistoryRow) -> str:
        """Return the stable document source ID for a message-history row."""
        turn_id = row.get("turn_id")
        if turn_id:
            return f"message_turn:{turn_id}"
        return f"message_row:{row['internal_id']}"

    def _build_history_query_conditions(
        self,
        query: MessageHistoryQuery,
        *,
        include_text_query: bool = True,
    ) -> list[ColumnElement[bool]]:
        """Build SQLAlchemy filter conditions for structured message-history queries."""
        conditions: list[ColumnElement[bool]] = []
        if not query.include_internal:
            conditions.append(_visible_message_condition())
        if query.scope == "all_accessible":
            raise MessageHistoryAccessDeniedError(
                "The all_accessible scope is not enabled for message history."
            )
        if query.scope == "current_conversation":
            if not query.current_conversation_id:
                raise MessageHistoryAccessDeniedError(
                    "current_conversation scope requires the current conversation."
                )
            if (
                query.conversation_id is not None
                and query.conversation_id != query.current_conversation_id
            ):
                raise MessageHistoryAccessDeniedError(
                    "current_conversation scope cannot query another conversation."
                )
            conditions.append(
                message_history_table.c.conversation_id == query.current_conversation_id
            )
            if query.interface_type:
                conditions.append(
                    message_history_table.c.interface_type == query.interface_type
                )
        elif query.scope == "same_user":
            if query.current_user_id:
                user_access_conditions = [
                    message_history_table.c.user_id == query.current_user_id
                ]
                if query.current_conversation_id and (
                    query.conversation_id is None
                    or query.conversation_id == query.current_conversation_id
                ):
                    user_access_conditions.append(
                        and_(
                            message_history_table.c.user_id.is_(None),
                            message_history_table.c.conversation_id
                            == query.current_conversation_id,
                        )
                    )
                conditions.append(or_(*user_access_conditions))
                if query.conversation_id:
                    conditions.append(
                        message_history_table.c.conversation_id == query.conversation_id
                    )
            else:
                if not query.current_conversation_id:
                    raise MessageHistoryAccessDeniedError(
                        "same_user scope requires a user_id or current conversation."
                    )
                if (
                    query.conversation_id is not None
                    and query.conversation_id != query.current_conversation_id
                ):
                    raise MessageHistoryAccessDeniedError(
                        "same_user scope without a user_id cannot query another conversation."
                    )
                conditions.append(
                    message_history_table.c.conversation_id
                    == query.current_conversation_id
                )
            if query.interface_type:
                conditions.append(
                    message_history_table.c.interface_type == query.interface_type
                )

        if query.roles:
            conditions.append(message_history_table.c.role.in_(query.roles))
        if query.tool_names:
            tool_name_conditions: list[ColumnElement[bool]] = [
                message_history_table.c.tool_name.in_(query.tool_names)
            ]
            for tool_name in query.tool_names:
                tool_name_conditions.append(
                    sa_cast(message_history_table.c.tool_calls, String).contains(
                        f'"name": "{tool_name}"'
                    )
                )
            conditions.append(or_(*tool_name_conditions))
        if query.start_time:
            conditions.append(message_history_table.c.timestamp >= query.start_time)
        if query.end_time:
            conditions.append(message_history_table.c.timestamp <= query.end_time)
        if query.has_attachments is True:
            conditions.append(message_history_table.c.attachments.is_not(None))
        elif query.has_attachments is False:
            conditions.append(message_history_table.c.attachments.is_(None))
        if query.has_error is True:
            conditions.append(
                or_(
                    message_history_table.c.role == "error",
                    message_history_table.c.error_traceback.is_not(None),
                )
            )
        elif query.has_error is False:
            conditions.append(
                and_(
                    message_history_table.c.role != "error",
                    message_history_table.c.error_traceback.is_(None),
                )
            )

        if query.processing_profile_id != "*":
            if query.processing_profile_id is None:
                conditions.append(
                    message_history_table.c.processing_profile_id.is_(None)
                )
            else:
                conditions.append(
                    message_history_table.c.processing_profile_id
                    == query.processing_profile_id
                )

        subconversation_condition = _subconversation_filter(query.subconversation_id)
        if subconversation_condition is not None:
            conditions.append(subconversation_condition)

        if include_text_query and query.query:
            if self._db.engine.dialect.name == "postgresql":
                postgres_text_query = sql_func.plainto_tsquery("english", query.query)
                like_pattern = f"%{query.query.lower()}%"
                conditions.append(
                    or_(
                        sql_func.to_tsvector(
                            "english",
                            sql_func.coalesce(message_history_table.c.content, ""),
                        ).op("@@")(postgres_text_query),
                        sql_func.to_tsvector(
                            "english",
                            sql_func.coalesce(
                                sa_cast(message_history_table.c.tool_calls, String),
                                "",
                            ),
                        ).op("@@")(postgres_text_query),
                        sql_func.lower(message_history_table.c.tool_name).like(
                            like_pattern
                        ),
                        sql_func.lower(
                            sa_cast(message_history_table.c.tool_calls, String)
                        ).like(like_pattern),
                    )
                )
            else:
                like_pattern = f"%{query.query.lower()}%"
                conditions.append(
                    or_(
                        sql_func.lower(message_history_table.c.content).like(
                            like_pattern
                        ),
                        sql_func.lower(message_history_table.c.tool_name).like(
                            like_pattern
                        ),
                        sql_func.lower(
                            sa_cast(message_history_table.c.tool_calls, String)
                        ).like(like_pattern),
                    )
                )

        return conditions

    @staticmethod
    def summarize_message_row(row: MessageHistoryRow) -> MessageHistorySummary:
        """Convert a message row into compact JSON-safe metadata for tools."""
        tool_calls = row.get("tool_calls")
        summarized_tool_calls: list[MessageHistoryToolCallSummary] = []
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if isinstance(tool_call, ToolCallItem):
                    summarized_tool_calls.append({
                        "id": tool_call.id,
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    })

        attachments = row.get("attachments")
        attachment_count = len(attachments) if isinstance(attachments, list) else 0
        return {
            "message_id": row["internal_id"],
            "interface_type": row["interface_type"],
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
            "thread_root_id": row["thread_root_id"],
            "timestamp": row["timestamp"].isoformat(),
            "role": row["role"],
            "user_id": row["user_id"],
            "processing_profile_id": row["processing_profile_id"],
            "subconversation_id": row["subconversation_id"],
            "content": row["content"],
            "tool_name": row["tool_name"],
            "tool_call_id": row["tool_call_id"],
            "tool_calls": summarized_tool_calls,
            "has_error": row["role"] == "error" or bool(row["error_traceback"]),
            "has_attachments": attachment_count > 0,
            "attachment_count": attachment_count,
        }

    async def add_message(
        self,
        message: LLMMessage,
        *,
        interface_type: str,
        conversation_id: str,
        timestamp: datetime,
        interface_message_id: str | None = None,
        turn_id: str | None = None,
        thread_root_id: int | None = None,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
        user_id: str | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        attachments: list[MessageAttachmentMetadata] | None = None,
        is_internal: bool = False,
    ) -> int | None:
        """
        Stores a typed LLMMessage in the history table.

        The message content fields (role, content, tool_calls, etc.) are extracted from the
        LLMMessage object. Metadata fields (interface_type, conversation_id, etc.) are passed
        as keyword arguments.

        Args:
            message: The typed LLMMessage to store.
            interface_type: Type of interface (e.g., 'telegram', 'web')
            conversation_id: Unique conversation identifier
            timestamp: Message timestamp (must be timezone-aware)
            interface_message_id: Interface-specific message ID
            turn_id: UUID linking messages within a turn
            thread_root_id: ID of the first message in the thread
            processing_profile_id: Processing profile used
            subconversation_id: Subconversation ID for delegation
            user_id: User identifier
            reasoning_info: LLM reasoning/usage info
            is_internal: Hide this row from user-facing history while keeping it
                available to LLM context.

        Returns:
            The stored message data including generated internal_id, or None on error
        """
        if timestamp.tzinfo is None:
            raise ValueError("Timestamp must be timezone-aware")

        # Extract message content fields from the typed LLMMessage
        role = message.role
        content: str | None = None
        tool_calls: list[ToolCallItem] | None = None
        tool_call_id: str | None = None
        tool_name: str | None = None
        error_traceback: str | None = None
        provider_metadata: ProviderMetadataDict | GeminiProviderMetadata | None = None
        taint_metadata = getattr(message, "taint_metadata", None)
        if taint_metadata is None and role == "user":
            taint_metadata = TurnTaintState.empty().to_metadata()
        elif taint_metadata is None and role in _HISTORY_ROLES_REQUIRING_TAINT_METADATA:
            # Regression guard: every write path for taint-applicable roles must
            # supply runtime taint metadata. A row persisted without it is
            # escalated to unknown_external at read time, permanently tainting
            # the conversation, so surface the gap loudly (but keep writing).
            logger.error(
                "taint_metadata_missing_at_write: persisting %s-role message "
                "history row without runtime taint metadata "
                "(interface=%s conversation=%s tool=%s); it will be treated as "
                "unknown_external at read time.",
                role,
                interface_type,
                conversation_id,
                getattr(message, "name", None),
            )

        raw_content = getattr(message, "content", None)
        if raw_content is None or isinstance(raw_content, str):
            content = raw_content
        elif isinstance(raw_content, list):
            content = json.dumps([
                part.model_dump(mode="json") if hasattr(part, "model_dump") else part
                for part in raw_content
            ])
        else:
            content = json.dumps(raw_content)

        if isinstance(message, AssistantMessage):
            tool_calls = message.tool_calls
            provider_metadata = message.provider_metadata
        elif isinstance(message, ToolMessage):
            tool_call_id = message.tool_call_id
            tool_name = message.name
            error_traceback = message.error_traceback
            provider_metadata = message.provider_metadata
            if attachments is None:
                attachments = message.attachments  # type: ignore[assignment]  # ToolAttachmentMetadata (TypedDict) is a dict at runtime
        elif isinstance(message, ErrorMessage):
            error_traceback = message.error_traceback

        return await self._insert_message(
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id=interface_message_id,
            turn_id=turn_id,
            thread_root_id=thread_root_id,
            timestamp=timestamp,
            role=role,
            content=content,
            tool_calls=tool_calls,
            reasoning_info=reasoning_info,
            error_traceback=error_traceback,
            tool_call_id=tool_call_id,
            processing_profile_id=processing_profile_id,
            subconversation_id=subconversation_id,
            user_id=user_id,
            attachments=attachments,
            is_internal=is_internal,
            tool_name=tool_name,
            provider_metadata=provider_metadata,
            taint_metadata=taint_metadata,
        )

    async def _insert_message(
        self,
        interface_type: str,
        conversation_id: str,
        interface_message_id: str | None,
        turn_id: str | None,
        thread_root_id: int | None,
        timestamp: datetime,
        role: str,
        content: str | None,
        tool_calls: list[ToolCallItem] | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        error_traceback: str | None = None,
        tool_call_id: str | None = None,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
        user_id: str | None = None,
        attachments: list[MessageAttachmentMetadata] | None = None,
        is_internal: bool = False,
        tool_name: str | None = None,
        provider_metadata: ProviderMetadataDict | GeminiProviderMetadata | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> int | None:
        """
        Internal method that serializes and inserts a message into the database.

        Returns the generated internal_id, or None on error.
        """
        # Serialize tool_calls for JSON storage
        serialized_tool_calls = None
        if tool_calls:
            serialized_tool_calls = []
            for tc in tool_calls:
                if not isinstance(tc, ToolCallItem):
                    raise TypeError(
                        f"tool_calls must contain ToolCallItem objects; got {type(tc)}"
                    )
                if isinstance(tc.provider_metadata, GeminiProviderMetadata):
                    tc_dict = asdict(tc)
                    tc_dict["provider_metadata"] = tc.provider_metadata.to_dict()
                    serialized_tool_calls.append(tc_dict)
                else:
                    tc_dict = asdict(tc)
                    serialized_tool_calls.append(tc_dict)

        serialized_provider_metadata = None
        if provider_metadata:
            if isinstance(provider_metadata, GeminiProviderMetadata):
                serialized_provider_metadata = provider_metadata.to_dict()
            else:
                serialized_provider_metadata = provider_metadata

        values = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "interface_message_id": interface_message_id,
            "turn_id": turn_id,
            "thread_root_id": thread_root_id,
            "timestamp": timestamp,
            "role": role,
            "content": content,
            "tool_calls": serialized_tool_calls,
            "reasoning_info": reasoning_info,
            "tool_call_id": tool_call_id,
            "error_traceback": error_traceback,
            "processing_profile_id": processing_profile_id,
            "subconversation_id": subconversation_id,
            "attachments": attachments,
            "is_internal": is_internal,
            "tool_name": tool_name,
            "provider_metadata": serialized_provider_metadata,
            "taint_metadata_json": taint_metadata,
            "taint_metadata_version": TAINT_METADATA_VERSION
            if taint_metadata is not None
            else None,
            "user_id": user_id,
        }

        # Remove None values except for fields that explicitly allow None
        values = {
            k: v
            for k, v in values.items()
            if v is not None
            or k
            in {
                "content",
                "interface_message_id",
                "turn_id",
                "thread_root_id",
                "tool_calls",
                "reasoning_info",
                "tool_call_id",
                "error_traceback",
                "processing_profile_id",
                "subconversation_id",
                "attachments",
                "is_internal",
                "tool_name",
                "provider_metadata",
                "taint_metadata_json",
                "taint_metadata_version",
                "user_id",
            }
        }

        try:
            stmt = (
                insert(message_history_table)
                .values(**values)
                .returning(message_history_table.c.internal_id)
            )
            result = await self._db.execute_with_retry(stmt)
            row = result.one()  # type: ignore[attr-defined]
            internal_id = row[0]

            self._logger.info(
                f"Added message to history: role={role}, "
                f"interface={interface_type}, internal_id={internal_id}"
            )

        except SQLAlchemyError as e:
            self._logger.error(f"Failed to add message to history: {e}", exc_info=True)
            return None

        await self._enqueue_message_history_indexing_task(
            internal_id=internal_id,
            turn_id=turn_id,
        )
        return internal_id

    async def _enqueue_message_history_indexing_task(
        self,
        *,
        internal_id: int,
        turn_id: str | None,
    ) -> None:
        """Queue indexing for newly persisted message history."""
        payload: dict[str, object] = {"limit": 50}
        if turn_id:
            payload["turn_id"] = turn_id
        else:
            payload["internal_id"] = internal_id

        await self._db.tasks.enqueue(
            task_id=f"index_message_history_{uuid.uuid4()}",
            task_type="index_message_history_batch",
            payload=payload,
        )

    async def get_recent(
        self,
        interface_type: str,
        conversation_id: str,
        limit: int | None = None,
        max_age: timedelta | None = None,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
        current_time: datetime | None = None,
    ) -> list[LLMMessage]:
        """
        Retrieves recent message history for a conversation.

        Args:
            interface_type: Type of interface
            conversation_id: Conversation identifier
            limit: Maximum number of messages to return
            max_age: Maximum age of messages to return
            processing_profile_id: Filter by processing profile
            subconversation_id: Filter by subconversation ID
            current_time: Current time for calculating cutoff (defaults to now)

        Returns:
            List of typed LLMMessage objects in chronological order
        """
        now = current_time or datetime.now(UTC)
        cutoff = now - max_age if max_age else now - timedelta(hours=24)

        conditions = [
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.timestamp >= cutoff,
        ]

        if processing_profile_id:
            conditions.append(
                message_history_table.c.processing_profile_id == processing_profile_id
            )

        # Filter by subconversation_id: None means main conversation only (IS NULL)
        if subconversation_id is None:
            conditions.append(message_history_table.c.subconversation_id.is_(None))
        else:
            conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )

        # First, order by timestamp descending to get the most recent messages
        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.desc(),
                message_history_table.c.internal_id.desc(),
            )
        )

        if limit:
            stmt = stmt.limit(limit)

        rows = await self._db.fetch_all(stmt)

        # Reverse the results to return them in chronological order (oldest first)
        # This ensures we get the N most recent messages but present them chronologically
        rows.reverse()

        return [self._process_message_row(row) for row in rows]

    async def get_recent_with_metadata(
        self,
        interface_type: str,
        conversation_id: str,
        limit: int | None = None,
        max_age: timedelta | None = None,
    ) -> list[MessageHistoryRow]:
        """
        Retrieves recent messages with database metadata (timestamp, internal_id, etc.).

        This method returns the full database rows including both message content and
        metadata fields. Use this when you need access to timestamps or other metadata.
        For just message content, use get_recent() which returns typed LLMMessage objects.

        Args:
            interface_type: Type of interface
            conversation_id: Conversation identifier
            limit: Maximum number of messages to return
            max_age: Maximum age of messages to return

        Returns:
            List of MessageHistoryRow with message content + metadata
        """
        if max_age:
            cutoff = datetime.now(UTC) - max_age
        else:
            cutoff = datetime.now(UTC) - timedelta(hours=24)

        stmt = (
            select(message_history_table)
            .where(
                message_history_table.c.interface_type == interface_type,
                message_history_table.c.conversation_id == conversation_id,
                message_history_table.c.timestamp >= cutoff,
            )
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
        )

        if limit:
            stmt = stmt.limit(limit)

        rows = await self._db.fetch_all(stmt)
        return [cast("MessageHistoryRow", dict(row)) for row in rows]

    async def get_recent_with_typed_metadata(
        self,
        interface_type: str,
        conversation_id: str,
        limit: int | None = None,
        max_age: timedelta | None = None,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
        thread_root_id: int | None = None,
    ) -> list[MessageWithMetadata]:
        """
        Retrieves recent messages with both typed content and database metadata.

        Use this when you need to filter by metadata fields (interface_message_id, timestamp)
        while maintaining type safety. For LLM context, use get_recent() instead.

        Args:
            interface_type: Type of interface
            conversation_id: Conversation identifier
            limit: Maximum number of messages to return
            max_age: Maximum age of messages to return
            processing_profile_id: Filter by processing profile
            subconversation_id: Filter by subconversation ID
            thread_root_id: Filter by thread root ID

        Returns:
            List of MessageWithMetadata objects in chronological order
        """
        cutoff = datetime.now(UTC) - max_age if max_age else None

        query = select(message_history_table).where(
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
        )

        if cutoff:
            query = query.where(message_history_table.c.timestamp >= cutoff)
        if processing_profile_id is not None:
            query = query.where(
                message_history_table.c.processing_profile_id == processing_profile_id
            )
        if subconversation_id is not None:
            query = query.where(
                message_history_table.c.subconversation_id == subconversation_id
            )
        if thread_root_id is not None:
            query = query.where(
                or_(
                    message_history_table.c.thread_root_id == thread_root_id,
                    message_history_table.c.internal_id == thread_root_id,
                )
            )
            self._logger.debug(
                f"Querying thread history for root {thread_root_id} with profile {processing_profile_id}"
            )

        query = query.order_by(
            message_history_table.c.timestamp.desc(),
            message_history_table.c.internal_id.desc(),
        )
        if limit:
            query = query.limit(limit)

        rows = await self._db.fetch_all(query)
        self._logger.debug(f"Fetched {len(rows)} rows for thread history query")
        rows = list(reversed(rows))  # Chronological order

        # Convert rows to MessageWithMetadata objects
        return [
            MessageWithMetadata(
                message=self._process_message_row(row),  # Returns typed LLMMessage
                internal_id=str(row["internal_id"]),
                interface_message_id=row["interface_message_id"],
                timestamp=row["timestamp"],
                conversation_id=row["conversation_id"],
                interface_type=row["interface_type"],
                user_id=row["user_id"],
                turn_id=row["turn_id"],
                thread_root_id=row["thread_root_id"],
            )
            for row in rows
        ]

    async def get_recent_tool_examples(
        self,
        *,
        interface_type: str,
        conversation_id: str,
        subconversation_id: str | None,
        tool_name: str,
        limit: int,
        user_id: str | None = None,
    ) -> list[ToolHistoryExample]:
        """Return recent successful tool examples from the same conversation or user."""
        if limit <= 0:
            return []

        same_conversation_conditions: list[ColumnElement[bool]] = [
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
        ]
        if subconversation_id is None:
            same_conversation_conditions.append(
                message_history_table.c.subconversation_id.is_(None)
            )
        else:
            same_conversation_conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )

        examples = await self._fetch_tool_examples_for_scope(
            tool_name=tool_name,
            limit=limit,
            conditions=tuple(same_conversation_conditions),
        )
        if examples or user_id is None:
            return examples

        same_user_conditions: list[ColumnElement[bool]] = [
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.user_id == user_id,
        ]
        if subconversation_id is None:
            same_user_conditions.append(
                message_history_table.c.subconversation_id.is_(None)
            )
        else:
            same_user_conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )

        return await self._fetch_tool_examples_for_scope(
            tool_name=tool_name,
            limit=limit,
            conditions=tuple(same_user_conditions),
        )

    async def _fetch_tool_examples_for_scope(
        self,
        *,
        tool_name: str,
        limit: int,
        conditions: tuple[ColumnElement[bool], ...],
    ) -> list[ToolHistoryExample]:
        """Fetch reconstructed tool examples for a specific query scope."""
        stmt = (
            select(message_history_table)
            .where(
                message_history_table.c.role == "tool",
                message_history_table.c.tool_name == tool_name,
                message_history_table.c.error_traceback.is_(None),
                *conditions,
            )
            .order_by(
                message_history_table.c.timestamp.desc(),
                message_history_table.c.internal_id.desc(),
            )
            .limit(limit * 4)
        )
        tool_rows = await self._db.fetch_all(stmt)

        examples: list[ToolHistoryExample] = []
        for tool_row in tool_rows:
            tool_message = self._process_message_row_as_dict(tool_row)
            if not self._is_tool_message_usable_for_example(tool_message):
                continue

            tool_call_id = tool_message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            tool_message_internal_id = cast("int", tool_message["internal_id"])
            tool_message_timestamp = cast("datetime", tool_message["timestamp"])

            assistant_conditions: list[ColumnElement[bool]] = [
                message_history_table.c.role == "assistant",
                message_history_table.c.interface_type
                == tool_message["interface_type"],
                message_history_table.c.conversation_id
                == tool_message["conversation_id"],
                message_history_table.c.tool_calls.is_not(None),
                or_(
                    message_history_table.c.timestamp < tool_message_timestamp,
                    (message_history_table.c.timestamp == tool_message_timestamp)
                    & (message_history_table.c.internal_id < tool_message_internal_id),
                ),
            ]
            tool_message_subconversation_id = tool_message.get("subconversation_id")
            if tool_message_subconversation_id is None:
                assistant_conditions.append(
                    message_history_table.c.subconversation_id.is_(None)
                )
            else:
                assistant_conditions.append(
                    message_history_table.c.subconversation_id
                    == tool_message_subconversation_id
                )

            assistant_stmt = (
                select(message_history_table)
                .where(*assistant_conditions)
                .order_by(
                    message_history_table.c.timestamp.desc(),
                    message_history_table.c.internal_id.desc(),
                )
                .limit(_MAX_ASSISTANT_ROWS_PER_TOOL_EXAMPLE)
            )
            assistant_rows = await self._db.fetch_all(assistant_stmt)

            arguments = self._extract_tool_arguments_from_assistant_rows(
                assistant_rows=tuple(dict(row) for row in assistant_rows),
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )
            if arguments is None:
                continue

            examples.append(
                ToolHistoryExample(
                    tool_name=tool_name,
                    arguments=arguments,
                    result_content=cast("str", tool_message["content"]),
                    conversation_id=cast("str", tool_message["conversation_id"]),
                    tool_call_id=tool_call_id,
                )
            )
            if len(examples) >= limit:
                break

        return examples

    @staticmethod
    def _is_tool_message_usable_for_example(
        tool_message: Mapping[str, Any],
    ) -> bool:
        """Return whether a persisted tool message is suitable for few-shot prompting."""
        content = tool_message.get("content")
        if not isinstance(content, str) or not content.strip():
            return False
        if content.startswith("Error:"):
            return False

        attachments = tool_message.get("attachments")
        if isinstance(attachments, list) and attachments:
            return False

        return len(content) <= 4000

    def _extract_tool_arguments_from_assistant_rows(
        self,
        *,
        assistant_rows: tuple[Mapping[str, Any], ...],
        tool_call_id: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - Reconstructed tool arguments are dynamic JSON keyed by tool schema
    ) -> dict[str, Any] | None:
        """Find tool arguments for a persisted tool call by scanning assistant tool_calls."""
        for assistant_row in assistant_rows:
            assistant_message = self._process_message_row_as_dict(assistant_row)
            tool_calls = assistant_message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue

            for tool_call in tool_calls:
                if not isinstance(tool_call, ToolCallItem):
                    continue
                if tool_call.id != tool_call_id or tool_call.function.name != tool_name:
                    continue

                raw_arguments = tool_call.function.arguments
                if isinstance(raw_arguments, dict):
                    return cast("dict[str, Any]", raw_arguments)
                if not isinstance(raw_arguments, str):
                    return None

                try:
                    parsed_arguments = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed_arguments, dict):
                    return cast("dict[str, Any]", parsed_arguments)
                return None

        return None

    async def get_by_interface_id(
        self,
        interface_type: str,
        interface_message_id: str,
    ) -> LLMMessage | None:
        """
        Retrieves a message by its interface-specific ID.

        Args:
            interface_type: Type of interface
            interface_message_id: Interface-specific message ID

        Returns:
            Typed LLMMessage or None if not found
        """
        stmt = select(message_history_table).where(
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.interface_message_id == interface_message_id,
        )

        row = await self._db.fetch_one(stmt)
        return self._process_message_row(row) if row else None

    async def get_row_by_interface_id(
        self,
        interface_type: str,
        interface_message_id: str,
    ) -> MessageHistoryRow | None:
        """
        Retrieves raw database row by interface-specific ID, including metadata.

        This method returns the full database row including metadata fields like
        internal_id, thread_root_id, and processing_profile_id that are not part
        of the LLMMessage content types.

        Args:
            interface_type: Type of interface
            interface_message_id: Interface-specific message ID

        Returns:
            Dict with all database fields including metadata, or None if not found
        """
        stmt = select(message_history_table).where(
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.interface_message_id == interface_message_id,
        )

        row = await self._db.fetch_one(stmt)
        return cast("MessageHistoryRow", dict(row)) if row else None

    async def get_row_by_internal_id(
        self,
        internal_id: int,
    ) -> MessageHistoryRow | None:
        """
        Retrieves raw database row by internal database ID, including metadata.

        Args:
            internal_id: Internal database ID

        Returns:
            Dict with all database fields including metadata, or None if not found
        """
        stmt = select(message_history_table).where(
            message_history_table.c.internal_id == internal_id,
        )

        row = await self._db.fetch_one(stmt)
        return cast("MessageHistoryRow", dict(row)) if row else None

    async def get_merged_taint_metadata_for_subconversation(
        self,
        *,
        interface_type: str,
        conversation_id: str,
        subconversation_id: str | None,
    ) -> TaintMetadata | None:
        """Return merged taint metadata for every message in a scope.

        Used to recover the accumulated taint of a completed (sub)conversation —
        e.g. a delegated run that stopped after a tainted tool result but before
        persisting a final assistant row. Returns ``None`` only when the scope has
        no rows, so callers can fall back conservatively.
        """
        stmt = select(message_history_table).where(
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
        )
        if subconversation_id is None:
            stmt = stmt.where(message_history_table.c.subconversation_id.is_(None))
        else:
            stmt = stmt.where(
                message_history_table.c.subconversation_id == subconversation_id
            )
        stmt = stmt.order_by(message_history_table.c.internal_id.asc())

        rows = await self._db.fetch_all(stmt)
        if not rows:
            return None
        messages = [self._process_message_row(row) for row in rows]
        return merge_history_taint(messages).to_metadata()

    async def get_user_row_by_turn_id(
        self,
        turn_id: str,
    ) -> MessageHistoryRow | None:
        """Retrieves the user message row that started a processing turn."""
        stmt = (
            select(message_history_table)
            .where(
                message_history_table.c.turn_id == turn_id,
                message_history_table.c.role == "user",
            )
            .order_by(message_history_table.c.internal_id.asc())
            .limit(1)
        )

        row = await self._db.fetch_one(stmt)
        return cast("MessageHistoryRow", dict(row)) if row else None

    async def has_terminal_reply_for_turn(self, turn_id: str) -> bool:
        """Whether the turn has a TERMINAL assistant row — a final reply, stopped
        marker, or error message.

        A terminal assistant row carries no tool_calls; an intermediate
        tool-calling iteration (preamble text + tool_calls) is NOT terminal, so a
        turn that crashed after such a row but before the final reply is still
        considered incomplete. Distinguishes a finished turn (reload shows the
        result) from one interrupted by a crash/restart mid-turn (no result).

        The tool_calls JSON column stores a None value as JSON ``null`` rather
        than SQL NULL, so a ``tool_calls IS NULL`` predicate wouldn't match it
        across engines; check the deserialized value in Python instead.
        """
        stmt = select(message_history_table.c.tool_calls).where(
            message_history_table.c.turn_id == turn_id,
            message_history_table.c.role == "assistant",
        )
        rows = await self._db.fetch_all(stmt)
        return any(not row["tool_calls"] for row in rows)

    async def get_interface_type_for_conversation(
        self, conversation_id: str
    ) -> str | None:
        """
        Get the interface_type for a conversation by checking message history.

        This is used to detect which interface (telegram, web, etc.) a conversation
        belongs to, enabling cross-interface message routing.

        Args:
            conversation_id: The conversation identifier

        Returns:
            The interface_type string (e.g., "telegram", "web") or None if not found
        """
        stmt = (
            select(message_history_table.c.interface_type)
            .where(message_history_table.c.conversation_id == conversation_id)
            .limit(1)
        )

        row = await self._db.fetch_one(stmt)
        return row["interface_type"] if row else None

    async def get_conversation_owner_ids(self, conversation_id: str) -> set[str]:
        """Return the distinct, non-null ``user_id``s of user messages in a
        conversation, across all interface types.

        Used for ownership/authorization checks: a conversation "belongs to"
        whoever has authored a user message in it. An empty set means the
        conversation has no persisted user messages yet (brand new).
        """
        stmt = (
            select(message_history_table.c.user_id)
            .where(
                message_history_table.c.conversation_id == conversation_id,
                message_history_table.c.role == "user",
                message_history_table.c.user_id.is_not(None),
            )
            .distinct()
        )
        rows = await self._db.fetch_all(stmt)
        return {row["user_id"] for row in rows if row["user_id"] is not None}

    async def get_by_turn_id(self, turn_id: str) -> list[LLMMessage]:
        """
        Retrieves all messages for a specific turn.

        Args:
            turn_id: The turn identifier

        Returns:
            List of typed LLMMessage objects in the turn
        """
        stmt = (
            select(message_history_table)
            .where(message_history_table.c.turn_id == turn_id)
            .order_by(message_history_table.c.timestamp.asc())
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    async def get_by_internal_ids(
        self,
        internal_ids: tuple[int, ...],
    ) -> list[LLMMessage]:
        """Retrieve typed messages by internal IDs in chronological order."""
        if not internal_ids:
            return []

        stmt = (
            select(message_history_table)
            .where(message_history_table.c.internal_id.in_(internal_ids))
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    async def get_by_thread_id(
        self,
        thread_root_id: int,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
    ) -> list[LLMMessage]:
        """
        Retrieves all messages in a thread.

        Args:
            thread_root_id: The root message ID of the thread
            processing_profile_id: Filter by processing profile

        Returns:
            List of typed LLMMessage objects in the thread, including the root message
        """
        # Include both the root message itself (where internal_id = thread_root_id)
        # and all child messages (where thread_root_id = thread_root_id)
        conditions = [
            or_(
                message_history_table.c.internal_id == thread_root_id,
                message_history_table.c.thread_root_id == thread_root_id,
            )
        ]

        if processing_profile_id:
            conditions.append(
                message_history_table.c.processing_profile_id == processing_profile_id
            )

        # Filter by subconversation_id to maintain isolation
        if subconversation_id is None:
            conditions.append(message_history_table.c.subconversation_id.is_(None))
        else:
            conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    async def update_interface_id(
        self, internal_id: int, interface_message_id: str
    ) -> None:
        """
        Updates the interface message ID for a message.

        Args:
            internal_id: Internal database ID
            interface_message_id: New interface message ID
        """
        stmt = (
            update(message_history_table)
            .where(message_history_table.c.internal_id == internal_id)
            .values(interface_message_id=interface_message_id)
        )

        result = await self._db.execute_with_retry(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]
            self._logger.warning(
                f"No message found with internal_id {internal_id} to update interface ID"
            )

    async def update_attachments(
        self,
        internal_id: int,
        attachments: list[MessageAttachmentMetadata] | None,
    ) -> None:
        """
        Updates the stored attachments for a message.

        Args:
            internal_id: Internal database ID
            attachments: Attachment metadata to store
        """
        stmt = (
            update(message_history_table)
            .where(message_history_table.c.internal_id == internal_id)
            .values(attachments=attachments)
        )

        result = await self._db.execute_with_retry(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy runtime API.
            self._logger.warning(
                f"No message found with internal_id {internal_id} to update attachments"
            )

    async def update_error_traceback(
        self, internal_id: int, error_traceback: str
    ) -> None:
        """
        Updates the error traceback for a message.

        Args:
            internal_id: Internal database ID
            error_traceback: Error traceback to store
        """
        stmt = (
            update(message_history_table)
            .where(message_history_table.c.internal_id == internal_id)
            .values(error_traceback=error_traceback)
        )

        result = await self._db.execute_with_retry(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]
            self._logger.warning(
                f"No message found with internal_id {internal_id} to update error traceback"
            )

    async def get_conversation_messages_paginated(
        self,
        conversation_id: str,
        before: datetime | None = None,
        after: datetime | None = None,
        limit: int = 50,
        include_subconversations: bool = True,
    ) -> tuple[list[MessageHistoryRow], bool, bool]:
        """
        Get messages for a conversation with timestamp-based pagination.

        Args:
            conversation_id: The conversation identifier
            before: Get messages before this timestamp (for loading earlier)
            after: Get messages after this timestamp (for loading newer)
            limit: Maximum number of messages to return
            include_subconversations: Include delegated subconversation rows

        Returns:
            Tuple of (messages, has_more_before, has_more_after)
        """
        conditions = [
            message_history_table.c.conversation_id == conversation_id,
            _visible_message_condition(),
        ]
        if not include_subconversations:
            conditions.append(message_history_table.c.subconversation_id.is_(None))

        # Add timestamp conditions
        if before:
            conditions.append(message_history_table.c.timestamp < before)
            timestamp_order = message_history_table.c.timestamp.desc()
            internal_id_order = message_history_table.c.internal_id.desc()
        elif after:
            conditions.append(message_history_table.c.timestamp > after)
            timestamp_order = message_history_table.c.timestamp.asc()
            internal_id_order = message_history_table.c.internal_id.asc()
        else:
            # Default: most recent messages
            timestamp_order = message_history_table.c.timestamp.desc()
            internal_id_order = message_history_table.c.internal_id.desc()

        # Fetch one extra message to determine if there are more
        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(timestamp_order, internal_id_order)
            .limit(limit + 1)
        )

        rows = await self._db.fetch_all(stmt)
        # Process rows to dicts with database fields preserved for API
        messages = [self._process_message_row_as_dict(row) for row in rows]

        # Check if we have more messages
        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]

        # If we fetched in DESC order (before or default), reverse for chronological display
        if before or not after:
            messages.reverse()

        # Determine has_more_before and has_more_after flags
        if before:
            has_more_before = has_more
            # If we're loading "before", there are newer messages only if we found any messages
            has_more_after = len(messages) > 0
        elif after:
            # Check if there are actually messages before the 'after' timestamp
            before_conditions = [
                message_history_table.c.conversation_id == conversation_id,
                message_history_table.c.timestamp < after,
                _visible_message_condition(),
            ]
            if not include_subconversations:
                before_conditions.append(
                    message_history_table.c.subconversation_id.is_(None)
                )
            check_before_stmt = (
                select(message_history_table.c.internal_id)
                .where(*before_conditions)
                .limit(1)
            )
            before_rows = await self._db.fetch_all(check_before_stmt)
            has_more_before = len(before_rows) > 0
            has_more_after = has_more
        else:
            # Default case: loading most recent
            has_more_before = has_more
            has_more_after = False

        return messages, has_more_before, has_more_after

    async def get_conversation_message_count(
        self, conversation_id: str, include_subconversations: bool = True
    ) -> int:
        """
        Get the total number of messages in a conversation.

        Args:
            conversation_id: The conversation identifier
            include_subconversations: Include delegated subconversation rows

        Returns:
            Total number of messages in the conversation
        """
        conditions = [
            message_history_table.c.conversation_id == conversation_id,
            _visible_message_condition(),
        ]
        if not include_subconversations:
            conditions.append(message_history_table.c.subconversation_id.is_(None))

        stmt = select(
            func.count(message_history_table.c.internal_id).label("count")
        ).where(*conditions)

        row = await self._db.fetch_one(stmt)
        return row["count"] if row else 0

    async def get_latest_user_profile_id(
        self, conversation_id: str, include_subconversations: bool = False
    ) -> str | None:
        """Return the processing profile of the most recent user message.

        Clients adopt this when reopening a conversation so the follow-up turn is
        sent under the profile the conversation's (profile-partitioned) history was
        produced under. The most recent *user* row is used — not a delegated
        assistant row — so a thread that handed off to another profile (e.g.
        ``complex_tasks`` delegating to ``engineer``) still resumes as the profile
        the user was actually talking to. Computed independently of any message
        page limit, so adoption works even when the last user message is many
        rows back in a long, tool-heavy conversation.
        """
        conditions = [
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.role == "user",
            message_history_table.c.processing_profile_id.is_not(None),
            _visible_message_condition(),
        ]
        if not include_subconversations:
            conditions.append(message_history_table.c.subconversation_id.is_(None))

        stmt = (
            select(message_history_table.c.processing_profile_id)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.desc(),
                message_history_table.c.internal_id.desc(),
            )
            .limit(1)
        )

        row = await self._db.fetch_one(stmt)
        return row["processing_profile_id"] if row else None

    async def get_messages_after(
        self,
        conversation_id: str,
        after: datetime,
        interface_type: str | None = None,
        limit: int = 100,
        subconversation_id: str | None = None,
    ) -> list[LLMMessage]:
        """
        Get messages created after a specific timestamp.

        Used for incremental sync in SSE and catch-up scenarios.

        Args:
            conversation_id: The conversation identifier
            after: Get messages created after this timestamp
            interface_type: Optional filter by interface type
            limit: Maximum number of messages to return (default 100)
            subconversation_id: Filter by subconversation. None means main conversation
                only; "*" includes all subconversations.

        Returns:
            List of typed LLMMessage objects in chronological order (oldest first)
        """
        conditions = [
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.timestamp > after,
            _visible_message_condition(),
        ]

        if interface_type:
            conditions.append(message_history_table.c.interface_type == interface_type)
        subconversation_condition = _subconversation_filter(subconversation_id)
        if subconversation_condition is not None:
            conditions.append(subconversation_condition)

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
            .limit(limit)
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    async def get_messages_after_as_dict(
        self,
        conversation_id: str,
        after: datetime,
        interface_type: str | None = None,
        limit: int = 100,
        subconversation_id: str | None = None,
    ) -> list[MessageHistoryRow]:
        """
        Get messages created after a specific timestamp as dicts with database fields.

        Used for SSE endpoints that need to send database metadata to the frontend.

        Args:
            conversation_id: The conversation identifier
            after: Get messages created after this timestamp
            interface_type: Optional filter by interface type
            limit: Maximum number of messages to return (default 100)
            subconversation_id: Filter by subconversation. None means main conversation
                only; "*" includes all subconversations.

        Returns:
            List of MessageHistoryRow in chronological order (oldest first)
        """
        conditions = [
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.timestamp > after,
            _visible_message_condition(),
        ]

        if interface_type:
            conditions.append(message_history_table.c.interface_type == interface_type)
        subconversation_condition = _subconversation_filter(subconversation_id)
        if subconversation_condition is not None:
            conditions.append(subconversation_condition)

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
            .limit(limit)
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row_as_dict(row) for row in rows]

    def _process_message_row(self, row: Mapping[str, Any]) -> LLMMessage:
        """
        Process a message row from the database with proper type deserialization.

        Deserializes JSON fields and converts provider-specific metadata
        to proper typed objects for type safety. Returns a dict that can be
        used with database fields and converted to LLMMessage when needed.

        Args:
            row: Raw database row (Mapping from SQLAlchemy fetch_all/fetch_one)

        Returns:
            Dictionary with properly deserialized complex types
        """
        # ast-grep-ignore: no-dict-any - intermediate dict from raw SQLAlchemy row, progressively deserialized to typed LLMMessage
        msg: dict[str, Any] = dict(row)

        # Handle tool_calls deserialization: JSON columns return dicts/lists directly
        if isinstance(msg.get("tool_calls"), str):
            try:
                msg["tool_calls"] = json.loads(cast("str", msg["tool_calls"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse tool_calls JSON for message {msg.get('internal_id')}"
                )
                msg["tool_calls"] = None

        # Deserialize provider_metadata within each tool call to typed objects
        if msg.get("tool_calls") and isinstance(msg["tool_calls"], list):
            tool_call_items = []
            for tc_dict in msg["tool_calls"]:
                if isinstance(tc_dict, ToolCallItem):
                    # Already a ToolCallItem, keep it as-is
                    tool_call_items.append(tc_dict)
                elif isinstance(tc_dict, dict):
                    # Deserialize provider_metadata if present
                    provider_metadata = tc_dict.get("provider_metadata")
                    if (
                        isinstance(provider_metadata, dict)
                        and provider_metadata.get("provider") == "google"
                    ):
                        provider_metadata = GeminiProviderMetadata.from_dict(
                            provider_metadata
                        )

                    # Create ToolCallItem with typed provider_metadata
                    tool_call_items.append(
                        ToolCallItem(
                            id=tc_dict["id"],
                            type=tc_dict["type"],
                            function=ToolCallFunction(
                                name=tc_dict["function"]["name"],
                                arguments=tc_dict["function"]["arguments"],
                            ),
                            provider_metadata=provider_metadata,
                        )
                    )
                else:
                    self._logger.warning(
                        f"Unexpected tool_call type: {type(tc_dict)}, skipping"
                    )
            msg["tool_calls"] = tool_call_items

        # Handle reasoning_info deserialization
        if isinstance(msg.get("reasoning_info"), str):
            try:
                msg["reasoning_info"] = json.loads(cast("str", msg["reasoning_info"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse reasoning_info JSON for message {msg.get('internal_id')}"
                )
                msg["reasoning_info"] = None

        # Handle attachments deserialization
        if isinstance(msg.get("attachments"), str):
            try:
                msg["attachments"] = json.loads(cast("str", msg["attachments"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse attachments JSON for message {msg.get('internal_id')}"
                )
                msg["attachments"] = None

        # Handle provider_metadata deserialization at message level
        provider_metadata = msg.get("provider_metadata")
        if provider_metadata:
            if isinstance(provider_metadata, str):
                # String case: parse JSON first
                try:
                    provider_metadata = json.loads(provider_metadata)
                except json.JSONDecodeError:
                    self._logger.warning(
                        f"Failed to parse provider_metadata JSON for message {msg.get('internal_id')}"
                    )
                    provider_metadata = None

            # Convert Google provider metadata to typed object for in-app use
            if (
                isinstance(provider_metadata, dict)
                and provider_metadata.get("provider") == "google"
            ):
                provider_metadata = GeminiProviderMetadata.from_dict(provider_metadata)

            msg["provider_metadata"] = provider_metadata

        msg["taint_metadata"] = _message_history_taint_metadata(msg)

        # Convert dict to typed LLMMessage
        return self._dict_to_typed_message(msg)

    def _process_message_row_as_dict(self, row: Mapping[str, Any]) -> MessageHistoryRow:
        """
        Process a message row and return as dict with all database fields preserved.

        This method deserializes complex types (tool_calls, provider_metadata) while
        keeping all database fields (internal_id, timestamp, user_id, etc.).
        Use this for API endpoints that need complete message data.

        Args:
            row: Raw database row (Mapping from SQLAlchemy fetch_all/fetch_one)

        Returns:
            Dictionary with deserialized complex types and all database fields
        """
        # ast-grep-ignore: no-dict-any - intermediate dict from raw SQLAlchemy row before casting to MessageHistoryRow
        msg: dict[str, Any] = dict(row)

        # Handle tool_calls deserialization: JSON columns return dicts/lists directly
        if isinstance(msg.get("tool_calls"), str):
            try:
                msg["tool_calls"] = json.loads(cast("str", msg["tool_calls"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse tool_calls JSON for message {msg.get('internal_id')}"
                )
                msg["tool_calls"] = None

        # Deserialize provider_metadata within each tool call to typed objects
        if msg.get("tool_calls") and isinstance(msg["tool_calls"], list):
            tool_call_items = []
            for tc_dict in msg["tool_calls"]:
                if isinstance(tc_dict, ToolCallItem):
                    # Already a ToolCallItem, keep it as-is
                    tool_call_items.append(tc_dict)
                elif isinstance(tc_dict, dict):
                    # Deserialize provider_metadata if present
                    provider_metadata = tc_dict.get("provider_metadata")
                    if (
                        isinstance(provider_metadata, dict)
                        and provider_metadata.get("provider") == "google"
                    ):
                        provider_metadata = GeminiProviderMetadata.from_dict(
                            provider_metadata
                        )

                    # Create ToolCallItem with typed provider_metadata
                    tool_call_items.append(
                        ToolCallItem(
                            id=tc_dict["id"],
                            type=tc_dict["type"],
                            function=ToolCallFunction(
                                name=tc_dict["function"]["name"],
                                arguments=tc_dict["function"]["arguments"],
                            ),
                            provider_metadata=provider_metadata,
                        )
                    )
                else:
                    self._logger.warning(
                        f"Unexpected tool_call type: {type(tc_dict)}, skipping"
                    )
            msg["tool_calls"] = tool_call_items

        # Handle reasoning_info deserialization
        if isinstance(msg.get("reasoning_info"), str):
            try:
                msg["reasoning_info"] = json.loads(cast("str", msg["reasoning_info"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse reasoning_info JSON for message {msg.get('internal_id')}"
                )
                msg["reasoning_info"] = None

        # Handle attachments deserialization
        if isinstance(msg.get("attachments"), str):
            try:
                msg["attachments"] = json.loads(cast("str", msg["attachments"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse attachments JSON for message {msg.get('internal_id')}"
                )
                msg["attachments"] = None

        # Handle provider_metadata deserialization at message level
        provider_metadata = msg.get("provider_metadata")
        if provider_metadata:
            if isinstance(provider_metadata, str):
                # String case: parse JSON first
                try:
                    provider_metadata = json.loads(provider_metadata)
                except json.JSONDecodeError:
                    self._logger.warning(
                        f"Failed to parse provider_metadata JSON for message {msg.get('internal_id')}"
                    )
                    provider_metadata = None

            # Keep message-level provider_metadata as dict for now
            # (it has a different structure than tool-call-level provider_metadata)
            msg["provider_metadata"] = provider_metadata

        msg["taint_metadata"] = _message_history_taint_metadata(msg)

        return cast("MessageHistoryRow", msg)

    # ast-grep-ignore: no-dict-any - intermediate dict from raw SQLAlchemy row, typed fields accessed by key
    def _dict_to_typed_message(self, msg: dict[str, Any]) -> LLMMessage:
        """
        Convert a processed message dict to a proper typed LLMMessage object.

        The dict should have been processed by _process_message_row to ensure
        proper deserialization of complex types (tool_calls as ToolCallItem objects,
        provider_metadata as typed objects, etc.).

        Args:
            msg: Dictionary with deserialized message data

        Returns:
            Appropriate typed LLMMessage (UserMessage, AssistantMessage, ToolMessage,
            SystemMessage, or ErrorMessage)

        Raises:
            ValueError: If role is unknown
        """
        role = msg.get("role")

        if role == "user":
            text_content_str = msg.get("content") or ""
            attachments = msg.get("attachments")

            # Reconstruct multimodal content from attachments if present
            # Attachments with content_url that are images/video/audio/PDF should be
            # included as content parts for the LLM
            if attachments and isinstance(attachments, list):
                multimodal_attachments = [
                    att
                    for att in attachments
                    if att.get("content_url")
                    and (
                        att.get("type") in {"image", "video", "audio", "document"}
                        or att.get("content_type") == "application/pdf"
                        or att.get("mime_type") == "application/pdf"
                    )
                ]

                if multimodal_attachments:
                    # Build multimodal content: text first, then attachment URLs
                    content_parts: list[ContentPart] = []
                    if text_content_str:
                        content_parts.append(
                            TextContentPart(type="text", text=text_content_str)
                        )
                    for att in multimodal_attachments:
                        content_parts.append(
                            ImageUrlContentPart(
                                type="image_url",
                                image_url={"url": att["content_url"]},
                            )
                        )

                    return UserMessage(
                        content=content_parts,
                        taint_metadata=msg.get("taint_metadata"),
                    )

            # No multimodal attachments - return simple text content
            return UserMessage(
                content=text_content_str,
                taint_metadata=msg.get("taint_metadata"),
            )
        elif role == "assistant":
            return AssistantMessage(
                content=msg.get("content"),
                tool_calls=msg.get("tool_calls"),
                provider_metadata=msg.get("provider_metadata"),
                taint_metadata=msg.get("taint_metadata"),
            )
        elif role == "tool":
            return ToolMessage(
                tool_call_id=msg.get("tool_call_id") or "",
                content=msg.get("content") or "",
                name=msg.get("tool_name") or "",
                error_traceback=msg.get("error_traceback"),
                taint_metadata=msg.get("taint_metadata"),
            )
        elif role == "system":
            content = msg.get("content") or ""
            if _is_delegation_wake_system_message(content):
                return UserMessage(content=_historical_delegation_wake_content(content))
            return SystemMessage(
                content=content,
            )
        elif role == "error":
            return ErrorMessage(
                content=msg.get("content") or "",
                error_traceback=msg.get("error_traceback"),
            )
        else:
            raise ValueError(f"Unknown message role: {role}")

    async def get_all_grouped(
        self,
        interface_type: str | None = None,
        conversation_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        include_subconversations: bool = True,
        include_internal: bool = False,
    ) -> dict[tuple[str, str], list[MessageHistoryRow]]:
        """
        Retrieves all message history, grouped by (interface_type, conversation_id) and ordered by timestamp.

        Args:
            interface_type: Filter by interface type
            conversation_id: Filter by conversation ID
            date_from: Filter messages after this date (inclusive)
            date_to: Filter messages before this date (inclusive)
            include_subconversations: Include delegated subconversation rows
            include_internal: Include rows hidden from user-facing history

        Returns:
            Dictionary mapping (interface_type, conversation_id) tuples to lists of messages
        """
        # Build query conditions
        conditions = []
        if not include_internal:
            conditions.append(_visible_message_condition())
        if interface_type:
            conditions.append(message_history_table.c.interface_type == interface_type)
        if conversation_id:
            conditions.append(
                message_history_table.c.conversation_id == conversation_id
            )
        if date_from:
            conditions.append(message_history_table.c.timestamp >= date_from)
        if date_to:
            conditions.append(message_history_table.c.timestamp <= date_to)
        if not include_subconversations:
            conditions.append(message_history_table.c.subconversation_id.is_(None))

        stmt = select(message_history_table)
        if conditions:
            stmt = stmt.where(*conditions)

        stmt = stmt.order_by(
            message_history_table.c.interface_type,
            message_history_table.c.conversation_id,
            message_history_table.c.timestamp,
            message_history_table.c.internal_id,  # For stable chronological order
        )

        rows = await self._db.fetch_all(stmt)

        grouped_history: dict[tuple[str, str], list[MessageHistoryRow]] = {}

        for row in rows:
            msg = self._process_message_row_as_dict(row)
            key = (row["interface_type"], row["conversation_id"])

            if key not in grouped_history:
                grouped_history[key] = []
            grouped_history[key].append(msg)

        return grouped_history

    async def get_conversation_summaries(
        self,
        interface_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
        conversation_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        include_subconversations: bool = True,
    ) -> tuple[list[ConversationSummaryRow], int]:
        """
        Get conversation summaries with pagination, optimized for performance.

        Args:
            interface_type: Filter by interface type (None for all interfaces)
            limit: Maximum number of conversations to return
            offset: Number of conversations to skip for pagination
            conversation_id: Filter by specific conversation ID
            date_from: Filter conversations with messages after this date
            date_to: Filter conversations with messages before this date
            include_subconversations: Include delegated subconversation rows

        Returns:
            Tuple of (summaries list, total count)
        """
        # Build base conditions
        base_conditions = []
        base_conditions.append(_visible_message_condition())
        base_conditions.append(message_history_table.c.role.in_(["user", "assistant"]))
        base_conditions.append(message_history_table.c.content.isnot(None))

        if interface_type:
            base_conditions.append(
                message_history_table.c.interface_type == interface_type
            )

        if conversation_id:
            base_conditions.append(
                message_history_table.c.conversation_id == conversation_id
            )

        if date_from:
            base_conditions.append(message_history_table.c.timestamp >= date_from)

        if date_to:
            base_conditions.append(message_history_table.c.timestamp <= date_to)

        if not include_subconversations:
            base_conditions.append(message_history_table.c.subconversation_id.is_(None))

        # Subquery to get the latest message id and count per conversation
        # We get the max internal_id within the max timestamp to handle timestamp collisions
        latest_msg_subq = (
            select(
                message_history_table.c.conversation_id,
                func.max(message_history_table.c.timestamp).label("max_timestamp"),
            )
            .where(*base_conditions)
            .group_by(message_history_table.c.conversation_id)
            .subquery()
        )

        # Get the max internal_id for messages with the latest timestamp
        latest_id_subq = (
            select(
                message_history_table.c.conversation_id,
                func.max(message_history_table.c.internal_id).label("max_id"),
            )
            .join(
                latest_msg_subq,
                (
                    message_history_table.c.conversation_id
                    == latest_msg_subq.c.conversation_id
                )
                & (
                    message_history_table.c.timestamp == latest_msg_subq.c.max_timestamp
                ),
            )
            .where(*base_conditions)
            .group_by(message_history_table.c.conversation_id)
            .subquery()
        )

        # Get message counts per conversation (without content filter)
        count_conditions = []
        count_conditions.append(_visible_message_condition())
        count_conditions.append(message_history_table.c.role.in_(["user", "assistant"]))

        if interface_type:
            count_conditions.append(
                message_history_table.c.interface_type == interface_type
            )

        if conversation_id:
            count_conditions.append(
                message_history_table.c.conversation_id == conversation_id
            )

        if date_from:
            count_conditions.append(message_history_table.c.timestamp >= date_from)

        if date_to:
            count_conditions.append(message_history_table.c.timestamp <= date_to)

        if not include_subconversations:
            count_conditions.append(
                message_history_table.c.subconversation_id.is_(None)
            )

        msg_count_subq = (
            select(
                message_history_table.c.conversation_id,
                func.count(message_history_table.c.internal_id).label("msg_count"),
            )
            .where(*count_conditions)
            .group_by(message_history_table.c.conversation_id)
            .subquery()
        )

        # Main query to get conversation summaries with the latest message content
        summaries_query = (
            select(
                message_history_table.c.conversation_id,
                message_history_table.c.content,
                message_history_table.c.timestamp,
                message_history_table.c.interface_type,  # Include interface_type in results
                msg_count_subq.c.msg_count.label("message_count"),
            )
            .join(
                latest_id_subq,
                message_history_table.c.internal_id == latest_id_subq.c.max_id,
            )
            .join(
                msg_count_subq,
                message_history_table.c.conversation_id
                == msg_count_subq.c.conversation_id,
            )
            .where(
                message_history_table.c.content.isnot(None),
            )
            .order_by(message_history_table.c.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )

        # Count query - count conversations that have messages with content
        count_subquery = (
            select(message_history_table.c.conversation_id)
            .where(*base_conditions)
            .distinct()
            .subquery()
        )
        count_query = select(func.count().label("count")).select_from(count_subquery)

        # Execute queries
        summaries_rows = await self._db.fetch_all(summaries_query)
        count_row = await self._db.fetch_one(count_query)
        total_count = count_row["count"] if count_row else 0

        # Process results
        summaries: list[ConversationSummaryRow] = []
        for row in summaries_rows:
            summaries.append(
                ConversationSummaryRow(
                    conversation_id=row["conversation_id"],
                    last_message=row["content"][:100] if row["content"] else "",
                    last_timestamp=row["timestamp"],
                    message_count=row["message_count"],
                    interface_type=row["interface_type"],
                )
            )

        return summaries, total_count
