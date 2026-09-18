"""Which conversations are due for a memory review, as one query.

See docs/design/conversation-memory.md, "Reviews are scheduled from state, not
from events". Nothing is enqueued when a message is persisted; a recurring
sweep evaluates this predicate against stored data every few minutes. That is
what makes the mechanism recover from any crash -- there is no in-flight state
to lose -- and what removes the race between a message landing and a review
completing: a message that arrives during a review leaves rows after the
watermark, and the next sweep sees them.

**What a row has to be to count, and which column says so.** Four filters,
each excluding a different source the design names:

- ``subconversation_id IS NULL`` excludes delegation subconversations,
  including the curator's own rows, which are persisted in an internal
  subconversation.
- ``interface_type`` in the contributing set excludes email intake (`email`),
  A2A (`a2a`), generic API clients (`api`), research sub-turns (`research`) and
  the spoken interfaces (`telephone`), which are read-only in v1 for reasons in
  the persistence layer rather than the design.
- ``processing_profile_id`` in the contributing profiles excludes every
  internal profile -- the engineer, the media analyst, the event handler -- and
  every profile an operator has not opted in, including a profile reached by a
  slash command inside an otherwise contributing chat.
- ``timestamp`` after that profile's enablement moment excludes everything said
  before contribution was turned on, so turning the feature on does not spend a
  burst of model calls surfacing months of old conversation.

``is_internal`` is not one of the four: an automation-triggered or callback turn
is persisted as a ``role='user'`` row the application wrote rather than a person
did, and such a row must not on its own make a conversation due. It is excluded
from the *user activity* count instead, which is the clause that decides whether
there is anything worth reviewing.

**Timestamps.** Every comparison binds a UTC-normalised value, because SQLite
stores a ``DateTime(timezone=True)`` column as the wall time of whatever aware
value was passed and hands it back naive. Production writers use UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.sql import functions as func

from family_assistant.storage.memory_review import memory_review_watermarks_table
from family_assistant.storage.message_history import message_history_table

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from family_assistant.memory.review_settings import MemoryReviewSettings
    from family_assistant.storage.database import DatabaseExecutor
    from family_assistant.storage.types import MessageHistoryRow

NO_WATERMARK = 0
"""The watermark of a conversation that has never been reviewed.

``message_history.internal_id`` starts at 1, so zero admits every row without
the predicate having to special-case a missing join row.
"""


class DueReason(StrEnum):
    """Which clause made a conversation due."""

    IDLE = "idle"
    """It has been quiet for its interface's idle window."""

    MAX_DEFERRAL = "max_deferral"
    """Its oldest unreviewed row has waited too long, quiet or not."""


@dataclass(frozen=True)
class DueConversation:
    """One conversation the sweep should enqueue a review for."""

    interface_type: str
    conversation_id: str
    watermark: int
    """The last reviewed ``internal_id``; the review covers rows after it."""
    first_eligible_internal_id: int
    last_eligible_internal_id: int
    last_eligible_at: datetime
    oldest_unreviewed_at: datetime
    user_row_count: int
    reason: DueReason


async def select_due_conversations(
    db: DatabaseExecutor,
    *,
    now: datetime,
    settings: MemoryReviewSettings,
    contributing_profiles: Mapping[str, datetime],
) -> list[DueConversation]:
    """Every conversation whose unreviewed stretch is ready to be reviewed.

    Args:
        db: The handle the grouped query runs on.
        now: The moment both windows are measured back from.
        settings: The windows, the deferral ceiling and the contributing
            interfaces.
        contributing_profiles: Each contributing profile and the moment
            contribution was turned on for it, as
            ``MemoryReviewRepository.get_enablement`` returns it intersected
            with the configured contributors.

    Returns:
        The due conversations, oldest unreviewed row first, so a backlog is
        worked through in the order it accumulated.
    """
    if not settings.enabled or not contributing_profiles:
        return []
    if not settings.contributing_interfaces:
        return []

    moment = now.astimezone(UTC)
    watermark = func.coalesce(
        memory_review_watermarks_table.c.last_reviewed_internal_id, NO_WATERMARK
    )
    last_at = func.max(message_history_table.c.timestamp)
    oldest_at = func.min(message_history_table.c.timestamp)

    idle_clauses = [
        sa.and_(
            message_history_table.c.interface_type == interface_type,
            last_at < moment - settings.idle_window(interface_type),
        )
        for interface_type in sorted(settings.contributing_interfaces)
    ]

    query = (
        sa
        .select(
            message_history_table.c.interface_type,
            message_history_table.c.conversation_id,
            watermark.label("watermark"),
            func.min(message_history_table.c.internal_id).label("first_internal_id"),
            func.max(message_history_table.c.internal_id).label("last_internal_id"),
            last_at.label("last_at"),
            oldest_at.label("oldest_at"),
            func.sum(_user_activity_indicator()).label("user_rows"),
        )
        .select_from(
            message_history_table.outerjoin(
                memory_review_watermarks_table,
                sa.and_(
                    memory_review_watermarks_table.c.interface_type
                    == message_history_table.c.interface_type,
                    memory_review_watermarks_table.c.conversation_id
                    == message_history_table.c.conversation_id,
                ),
            )
        )
        .where(
            _eligible_row_condition(
                settings=settings, contributing_profiles=contributing_profiles
            ),
            message_history_table.c.internal_id > watermark,
        )
        .group_by(
            message_history_table.c.interface_type,
            message_history_table.c.conversation_id,
            memory_review_watermarks_table.c.last_reviewed_internal_id,
        )
        .having(func.sum(_user_activity_indicator()) > 0)
        .having(
            sa.or_(
                *idle_clauses,
                oldest_at < moment - settings.max_deferral,
            )
        )
        .order_by(oldest_at.asc())
    )

    rows = await db.fetch_all(query)
    return [
        DueConversation(
            interface_type=row["interface_type"],
            conversation_id=row["conversation_id"],
            watermark=int(row["watermark"]),
            first_eligible_internal_id=int(row["first_internal_id"]),
            last_eligible_internal_id=int(row["last_internal_id"]),
            last_eligible_at=_as_utc(row["last_at"]),
            oldest_unreviewed_at=_as_utc(row["oldest_at"]),
            user_row_count=int(row["user_rows"]),
            reason=_reason(
                interface_type=row["interface_type"],
                last_at=_as_utc(row["last_at"]),
                moment=moment,
                settings=settings,
            ),
        )
        for row in rows
    ]


async def select_review_rows(
    db: DatabaseExecutor,
    *,
    interface_type: str,
    conversation_id: str,
    watermark: int,
    settings: MemoryReviewSettings,
    contributing_profiles: Mapping[str, datetime],
) -> Sequence[MessageHistoryRow]:
    """The eligible rows of one conversation after its watermark, in order.

    The same eligibility the due predicate applies, for one conversation, so
    the stretch a review reads can never contain a row the predicate did not
    count. The caller cuts it into a chunk on a turn boundary.
    """
    return await db.message_history.rows_matching(
        sa.and_(
            _eligible_row_condition(
                settings=settings, contributing_profiles=contributing_profiles
            ),
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.internal_id > watermark,
        )
    )


def _eligible_row_condition(
    *,
    settings: MemoryReviewSettings,
    contributing_profiles: Mapping[str, datetime],
) -> sa.ColumnElement[bool]:
    """The four filters in the module docstring, as one SQL condition."""
    within_enablement = sa.or_(*[
        sa.and_(
            message_history_table.c.processing_profile_id == profile_id,
            message_history_table.c.timestamp > enabled_at.astimezone(UTC),
        )
        for profile_id, enabled_at in sorted(contributing_profiles.items())
    ])
    return sa.and_(
        message_history_table.c.subconversation_id.is_(None),
        message_history_table.c.interface_type.in_(
            sorted(settings.contributing_interfaces)
        ),
        within_enablement,
    )


def _user_activity_indicator() -> sa.ColumnElement[int]:
    """1 for a row that is a person speaking, 0 for anything else.

    Summed rather than counted so the zero case is a row in the result with a
    zero in it, which the HAVING clause can reject, rather than an absent row.
    """
    return sa.case(
        (
            sa.and_(
                message_history_table.c.role == "user",
                message_history_table.c.is_internal.is_(False),
            ),
            1,
        ),
        else_=0,
    )


def _reason(
    *,
    interface_type: str,
    last_at: datetime,
    moment: datetime,
    settings: MemoryReviewSettings,
) -> DueReason:
    """Which clause admitted a row the query has already admitted."""
    if last_at < moment - settings.idle_window(interface_type):
        return DueReason.IDLE
    return DueReason.MAX_DEFERRAL


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp, as SQLite hands them back."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
