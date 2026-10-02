"""The script editor validates full definitions before a human-direct write."""

import pytest
from httpx import AsyncClient

from family_assistant.security.definition_records import definition_record_from_row
from family_assistant.security.taint import SourceTrustTier, TurnTaintState
from family_assistant.storage.database import Database


@pytest.mark.asyncio
async def test_create_script_validates_declared_inputs_and_stamps_user_edit(
    api_test_client: AsyncClient,
    api_db_context: Database,
) -> None:
    response = await api_test_client.post(
        "/api/scripts/",
        json={
            "name": "greet",
            "description": "Greeting",
            "script_code": "print(message)",
            "parameters_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    )

    assert response.status_code == 200
    script = await api_db_context.scripts.get_by_name("greet")
    assert script is not None
    assert script.parameters_schema == response.json()["parameters_schema"]
    record = definition_record_from_row(script.definition_record)
    assert record is not None
    assert (
        TurnTaintState.from_metadata(record.taint_metadata).max_tier
        is SourceTrustTier.TRUSTED_USER
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,schema",
    [
        ("def broken(", None),
        ("print('hello')", {"required": "message"}),
        ("print('hello')", {"properties": ["message"]}),
    ],
)
async def test_invalid_script_never_saved(
    api_test_client: AsyncClient,
    api_db_context: Database,
    code: str,
    schema: object,
) -> None:
    response = await api_test_client.post(
        "/api/scripts/",
        json={
            "name": "invalid",
            "description": "Invalid",
            "script_code": code,
            "parameters_schema": schema,
        },
    )

    assert response.status_code == 400
    assert await api_db_context.scripts.get_by_name("invalid") is None


@pytest.mark.asyncio
async def test_edit_script_preserves_identity_and_clears_parameter_schema(
    api_test_client: AsyncClient,
    api_db_context: Database,
) -> None:
    original = await api_db_context.scripts.save(
        "edit-me", "Original", "print('original')", {"type": "object"}
    )
    artifact = next(
        item
        for item in await api_db_context.artifact_review.list_all()
        if item.kind == "script"
    )

    response = await api_test_client.post(
        "/api/scripts/",
        json={
            "name": "edit-me",
            "description": "Updated",
            "script_code": "print('updated')",
            "parameters_schema": None,
            "expected_content_hash": artifact.content_hash,
        },
    )

    assert response.status_code == 200
    updated = await api_db_context.scripts.get_by_name("edit-me")
    assert updated is not None and updated.id == original.id
    assert updated.created_at == original.created_at
    assert (
        updated.script_code == "print('updated')" and updated.description == "Updated"
    )
    assert updated.parameters_schema is None


@pytest.mark.asyncio
async def test_new_script_cannot_overwrite_existing_name(
    api_test_client: AsyncClient,
    api_db_context: Database,
) -> None:
    await api_db_context.scripts.save("existing", "Original", "print('original')")

    response = await api_test_client.post(
        "/api/scripts/",
        json={
            "name": "existing",
            "description": "Replacement",
            "script_code": "print('replacement')",
        },
    )

    assert response.status_code == 409
    script = await api_db_context.scripts.get_by_name("existing")
    assert script is not None and script.description == "Original"


@pytest.mark.asyncio
async def test_editor_rejects_stale_script_content(
    api_test_client: AsyncClient,
    api_db_context: Database,
) -> None:
    await api_db_context.scripts.save("existing", "Original", "print('original')")
    artifact = next(
        item
        for item in await api_db_context.artifact_review.list_all()
        if item.kind == "script"
    )
    await api_db_context.scripts.save(
        "existing", "Concurrent edit", "print('concurrent')"
    )

    response = await api_test_client.post(
        "/api/scripts/",
        json={
            "name": "existing",
            "description": "Stale",
            "script_code": "print('stale')",
            "expected_content_hash": artifact.content_hash,
        },
    )

    assert response.status_code == 409
    script = await api_db_context.scripts.get_by_name("existing")
    assert script is not None and script.description == "Concurrent edit"
