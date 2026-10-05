"""The record of each history compaction event.

An event decides, once, how every completed turn up to it renders in the prompt
-- verbatim, compacted, or not at all -- and later requests read the decision
back rather than re-deciding, so the window only appends between events. Only
the decision is stored: the compacted rendering is derived from the message
rows, so nothing here carries conversation text or taint of its own. See
docs/design/history-compaction.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
)
from sqlalchemy.dialects.postgresql import JSONB

from family_assistant.storage.base import metadata

history_compaction_events_table = Table(
    "history_compaction_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("interface_type", String(50), nullable=False),
    Column("conversation_id", String(255), nullable=False),
    Column("processing_profile_id", String(255), nullable=False),
    Column("subconversation_id", String(36), nullable=True),
    # Every turn whose first row is at or below this id is decided by the
    # event, except the turn that ran it; later turns render verbatim.
    Column("boundary_internal_id", BigInteger, nullable=False),
    # The turn that was active at the event, which is always sent whole. Its
    # rows up to the boundary lose their bound thinking when the event changed
    # the prefix before them.
    Column("active_turn_key", String(64), nullable=True),
    Column("reason", String(32), nullable=False),
    # turn key -> {"mode": "verbatim" | "compacted", "strip_through": row id};
    # a decided turn absent from the map is no longer in the window.
    Column("decisions", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    # Whether the event changed what the prompt carries; an event that did
    # not still records the moment it was judged.
    Column("changed", Boolean, nullable=False),
    # Sizes and, where a relevance classifier ran, its answers -- what shadow
    # evaluation needs to judge the event afterwards.
    Column("details", JSON().with_variant(JSONB, "postgresql"), nullable=True),
    Index(
        "ix_history_compaction_events_scope",
        "conversation_id",
        "interface_type",
        "processing_profile_id",
        "subconversation_id",
        "id",
    ),
)


class TurnMode(StrEnum):
    VERBATIM = "verbatim"
    COMPACTED = "compacted"


@dataclass(frozen=True, slots=True)
class TurnDecision:
    """How a turn renders until the next event.

    ``strip_through`` drops the Anthropic thinking blocks from the turn's rows
    up to that row id. A block is bound to the exact prefix before it, so once
    an event has changed an earlier turn every later block would only be
    dropped by the API as a mismatch; stripping them here keeps
    ``prefix_binding_mismatch`` meaning a bug. It is a row id rather than a
    flag because the turn active at an event keeps the thinking it produces
    after the event, and a strip, once made, holds at every later event.
    """

    mode: TurnMode
    strip_through: int | None = None

    def to_json(self) -> dict[str, object]:
        return {"mode": self.mode.value, "strip_through": self.strip_through}

    @classmethod
    def from_json(cls, value: object) -> TurnDecision:
        if not isinstance(value, dict):
            raise ValueError(f"Malformed compaction decision: {value!r}")
        strip_through = value.get("strip_through")
        return cls(
            mode=TurnMode(value["mode"]),
            strip_through=strip_through if isinstance(strip_through, int) else None,
        )


@dataclass(frozen=True, slots=True)
class HistoryScope:
    """The rows one profile's window over a conversation is built from."""

    interface_type: str
    conversation_id: str
    processing_profile_id: str
    subconversation_id: str | None
