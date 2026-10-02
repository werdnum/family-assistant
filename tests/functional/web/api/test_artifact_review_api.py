"""Artifact review approvals bind complete content and preserve execution settings."""

from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from family_assistant.security.definition_records import (
    automation_definition_content,
    definition_record_from_row,
    listener_definition_content,
    resolve_definition_record,
    script_definition_content,
)
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.database import Database
from family_assistant.storage.events import event_listeners_table
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.artifact_review import ArtifactKind
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.scripts import scripts_table


def external_state() -> TurnTaintState:
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id="external",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="External authoring content",
        )
    )


async def seed_artifact(db: Database, kind: ArtifactKind) -> int:
    state = external_state()
    if kind == "note":
        await db.notes.add_or_update(
            "Review me",
            "External content",
            False,
            visibility_labels=["private"],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
            provenance=NoteProvenanceStamp.machine(state),
        )
    elif kind == "script":
        await db.scripts.save(
            "review-me",
            "External script",
            "print('hello')",
            {"type": "object", "properties": {"message": {"type": "string"}}},
            definition_taint_state=state,
        )
    elif kind == "event":
        await db.events.create_event_listener(
            name="Review me",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            condition_script="return True",
            conversation_id="original",
            interface_type="telegram",
            action_type="wake_llm",
            action_config={"context": "Full external instructions"},
            enabled=False,
            one_time=True,
            processing_profile_id="original-profile",
            created_by_user_id="original-user",
            definition_taint_state=state,
        )
    else:
        await db.schedule_automations.create(
            name="Review me",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            timezone=ZoneInfo("UTC"),
            conversation_id="original",
            interface_type="telegram",
            action_type="wake_llm",
            action_config={"context": "Full external instructions"},
            enabled=False,
            processing_profile_id="original-profile",
            created_by_user_id="original-user",
            definition_taint_state=state,
        )
    artifacts = await db.artifact_review.list_all()
    return next(artifact.id for artifact in artifacts if artifact.kind == kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["note", "script", "event", "schedule"])
async def test_confirm_artifact_preserves_content_and_records_approval(
    api_test_client: AsyncClient,
    api_db_context: Database,
    kind: ArtifactKind,
) -> None:
    artifact_id = await seed_artifact(api_db_context, kind)
    table = {
        "note": notes_table,
        "script": scripts_table,
        "event": event_listeners_table,
        "schedule": schedule_automations_table,
    }[kind]
    before = await api_db_context.fetch_one(
        select(table).where(table.c.id == artifact_id)
    )
    listing = await api_test_client.get("/api/artifacts/")
    artifact = next(
        item
        for item in listing.json()
        if item["kind"] == kind and item["id"] == artifact_id
    )

    response = await api_test_client.post(
        f"/api/artifacts/{kind}/{artifact_id}/confirm",
        json={"content_hash": artifact["content_hash"]},
    )

    assert response.status_code == 200
    confirmed = response.json()
    assert confirmed["content"] == artifact["content"]
    assert confirmed["disposition"] == "human_confirmed"
    assert confirmed["trust_tier"] == "machine_reviewed"
    after = await api_db_context.fetch_one(
        select(table).where(table.c.id == artifact_id)
    )
    assert before is not None and after is not None
    for field in before.keys() - {
        "definition_record",
        "provenance_metadata_json",
        "updated_at",
    }:
        assert after[field] == before[field], field
    if kind != "note":
        # Runtime's canonical content builders must match the UI's complete snapshot.
        if kind == "script":
            script = await api_db_context.scripts.get_by_name("review-me")
            assert script is not None
            content = script_definition_content(
                name=script.name,
                description=script.description,
                script_code=script.script_code,
                parameters_schema=script.parameters_schema,
            )
        elif kind == "event":
            listener = await api_db_context.events.get_event_listener_by_id(artifact_id)
            assert listener is not None
            content = listener_definition_content(
                name=listener["name"],
                description=listener["description"],
                source_id=listener["source_id"],
                match_conditions=listener["match_conditions"],
                action_type=listener["action_type"],
                action_config=listener["action_config"],
                condition_script=listener["condition_script"],
            )
        else:
            schedule = await api_db_context.schedule_automations.get_by_id(artifact_id)
            assert schedule is not None
            content = automation_definition_content(
                name=schedule["name"],
                description=schedule["description"],
                recurrence_rule=schedule["recurrence_rule"],
                action_type=schedule["action_type"],
                action_config=schedule["action_config"],
            )
        assert resolve_definition_record(after["definition_record"], content).resolved
        record = definition_record_from_row(after["definition_record"])
        assert record is not None and record.cures
    audits = await api_db_context.taint_audit_events.list_for_conversation("test_user")
    assert any(
        event["artifact_id"] == f"{kind}:{artifact_id}"
        and event["effective_outcome"] == "human_confirmed"
        for event in audits
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["note", "script", "event", "schedule"])
async def test_confirm_rejects_content_changed_since_display(
    api_test_client: AsyncClient,
    api_db_context: Database,
    kind: ArtifactKind,
) -> None:
    artifact_id = await seed_artifact(api_db_context, kind)
    artifact = next(
        item
        for item in await api_db_context.artifact_review.list_all()
        if item.kind == kind
    )
    table = {
        "note": notes_table,
        "script": scripts_table,
        "event": event_listeners_table,
        "schedule": schedule_automations_table,
    }[kind]
    field = "content" if kind == "note" else "description"
    await api_db_context.execute(
        update(table)
        .where(table.c.id == artifact_id)
        .values({field: "changed after display"})
    )

    response = await api_test_client.post(
        f"/api/artifacts/{kind}/{artifact_id}/confirm",
        json={"content_hash": artifact.content_hash},
    )

    assert response.status_code == 409
    assert not await api_db_context.taint_audit_events.list_for_conversation(
        "test_user"
    )


@pytest.mark.asyncio
async def test_confirmation_not_found(api_test_client: AsyncClient) -> None:
    response = await api_test_client.post(
        "/api/artifacts/script/999999/confirm", json={"content_hash": "missing"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_note_edit_does_not_inherit_confirmation_status(
    api_db_context: Database,
) -> None:
    artifact_id = await seed_artifact(api_db_context, "note")
    artifact = next(
        item
        for item in await api_db_context.artifact_review.list_all()
        if item.kind == "note"
    )
    await api_db_context.artifact_review.confirm(
        "note", artifact_id, content_hash=artifact.content_hash, user_id="user"
    )
    await api_db_context.notes.add_or_update(
        "Review me",
        "new machine content",
        False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    updated = next(
        item
        for item in await api_db_context.artifact_review.list_all()
        if item.kind == "note"
    )

    assert updated.disposition is None
    note = await api_db_context.notes.get_by_title(
        "Review me", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None and note.content == "new machine content"
