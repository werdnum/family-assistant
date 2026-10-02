"""Content-bound human confirmation of stored notes and executable definitions."""

import json
import uuid
from typing import Literal, cast

from pydantic import BaseModel
from sqlalchemy import Table, select, update

from family_assistant.security.definition_records import (
    CreationDisposition,
    DefinitionGateOutcome,
    GateLayer,
    GateProvenance,
    definition_content_hash,
    definition_record_from_row,
    legacy_authoring_taint_state,
    resolve_definition_record,
    stamp_definition,
)
from family_assistant.security.note_provenance import (
    NoteProvenanceStamp,
    stored_note_state,
)
from family_assistant.security.taint import (
    TurnTaintState,
)
from family_assistant.security.taint_audit import taint_audit_sources
from family_assistant.storage.database import DatabaseTransaction
from family_assistant.storage.events import event_listeners_table
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.scripts import scripts_table

ArtifactKind = Literal["note", "script", "event", "schedule"]

# These are the canonical definition fields hashed by definition_records.
_TABLES: dict[ArtifactKind, Table] = {
    "note": notes_table,
    "script": scripts_table,
    "event": event_listeners_table,
    "schedule": schedule_automations_table,
}
_FIELDS: dict[ArtifactKind, tuple[str, ...]] = {
    "note": (
        "title",
        "content",
        "include_in_prompt",
        "attachment_ids",
        "visibility_labels",
    ),
    "script": ("name", "description", "script_code", "parameters_schema"),
    "event": (
        "name",
        "description",
        "source_id",
        "match_conditions",
        "action_type",
        "action_config",
        "condition_script",
    ),
    "schedule": (
        "name",
        "description",
        "recurrence_rule",
        "action_type",
        "action_config",
    ),
}
_JSON_FIELDS = frozenset({
    "attachment_ids",
    "visibility_labels",
    "parameters_schema",
    "match_conditions",
    "action_config",
})


class ArtifactReview(BaseModel):
    """Complete reviewable content and its current trust status."""

    kind: ArtifactKind
    id: int
    name: str
    content: dict[str, object]
    content_hash: str
    trust_tier: str
    disposition: str | None


class ArtifactChangedError(Exception):
    """The content approved by the user is no longer the stored content."""


def _content(kind: ArtifactKind, row: dict[str, object]) -> dict[str, object]:
    content = {field: row[field] for field in _FIELDS[kind]}
    for field in _JSON_FIELDS & content.keys():
        value = content[field]
        if isinstance(value, str):
            content[field] = json.loads(value)
    return content


def _state(kind: ArtifactKind, row: dict[str, object]) -> TurnTaintState:
    if kind == "note":
        return stored_note_state(
            cast("dict[str, object] | None", row["provenance_metadata_json"]),
            title=cast("str", row["title"]),
        )
    record = definition_record_from_row(row["definition_record"])
    return (
        TurnTaintState.from_metadata(record.taint_metadata)
        if record is not None and record.matches(_content(kind, row))
        else legacy_authoring_taint_state()
    )


def _review(kind: ArtifactKind, row: dict[str, object]) -> ArtifactReview:
    content = _content(kind, row)
    state = _state(kind, row)
    disposition = None
    if kind == "note":
        if any(
            "user_confirmed" in source.labels
            and source.source_id == definition_content_hash(content)
            for source in state.sources
        ):
            disposition = "human_confirmed"
        tier = state.max_tier
    else:
        resolution = resolve_definition_record(
            stored_record=row["definition_record"], content=content
        )
        tier = resolution.tier
        record = definition_record_from_row(row["definition_record"])
        if record is not None and record.content_hash == definition_content_hash(
            content
        ):
            disposition = (
                record.disposition.value if record.disposition is not None else None
            )
    return ArtifactReview(
        kind=kind,
        id=cast("int", row["id"]),
        name=cast("str", row["title"] if kind == "note" else row["name"]),
        content=content,
        content_hash=definition_content_hash(content),
        trust_tier=tier.config_value,
        disposition=disposition,
    )


class ArtifactReviewRepository(BaseRepository):
    """Review and confirm individual artifacts without changing their definitions."""

    async def list_all(self) -> list[ArtifactReview]:
        artifacts = []
        for kind, table in _TABLES.items():
            rows = await self._db.fetch_all(select(table).order_by(table.c.id))
            artifacts.extend(_review(kind, row) for row in rows)
        return artifacts

    async def confirm(
        self, kind: ArtifactKind, artifact_id: int, *, content_hash: str, user_id: str
    ) -> ArtifactReview | None:
        """Confirm exactly the displayed content, with the stamp and audit committed together."""
        table = _TABLES[kind]

        async def body(txn: DatabaseTransaction) -> ArtifactReview | None:
            row = await txn.fetch_one(
                select(table).where(table.c.id == artifact_id).with_for_update()
            )
            if row is None:
                return None
            content = _content(kind, row)
            if definition_content_hash(content) != content_hash:
                raise ArtifactChangedError(
                    "Artifact changed. Reload and review it again."
                )
            state = _state(kind, row)
            event_id = str(uuid.uuid4())
            if kind == "note":
                await txn.notes.add_or_update(
                    cast("str", content["title"]),
                    cast("str", content["content"]),
                    cast("bool", content["include_in_prompt"]),
                    attachment_ids=cast("list[str]", content["attachment_ids"]),
                    visibility_labels=cast("list[str]", content["visibility_labels"]),
                    # ast-grep-ignore: no-unconstrained-note-write-policy - authenticated artifact review is a web admin surface, preserving existing labels
                    write_policy=NoteWritePolicy.UNCONSTRAINED,
                    provenance=NoteProvenanceStamp.user_confirmed(
                        content_hash=content_hash
                    ),
                )
            else:
                serialize_record = json.dumps if kind == "script" else dict
                definition_record = serialize_record(
                    stamp_definition(
                        content=content,
                        taint_state=state,
                        gate_outcome=DefinitionGateOutcome(
                            disposition=CreationDisposition.HUMAN_CONFIRMED,
                            gate=GateProvenance(
                                layer=GateLayer.CONFIRMATION,
                                mode="human_direct",
                                verdict_id=event_id,
                            ),
                        ),
                    ).to_dict()
                )
                await txn.execute(
                    update(table)
                    .where(table.c.id == artifact_id)
                    .values(definition_record=definition_record)
                )
            await txn.taint_audit_events.add(
                event_id=event_id,
                event_type="artifact_user_confirmation",
                conversation_id=user_id,
                turn_id=None,
                processing_profile_id=None,
                subconversation_id=None,
                tool_name="confirm_artifact",
                tool_call_id=None,
                sink_class=None,
                max_tier=state.max_tier.config_value,
                sources=taint_audit_sources(state),
                requested_outcome="human_confirmed",
                effective_outcome="human_confirmed",
                mode="human_direct",
                reason="Authenticated user reviewed and confirmed the complete stored content.",
                arguments_summary=None,
                artifact_id=f"{kind}:{artifact_id}",
            )
            confirmed = await txn.fetch_one(
                select(table).where(table.c.id == artifact_id)
            )
            assert confirmed is not None
            return _review(kind, confirmed)

        return await self._db.atomic(body)
