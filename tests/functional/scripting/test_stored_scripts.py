"""Functional tests for stored scripts tools.

Tests the CRUD operations and execution of stored scripts including:
- save_script_tool: Save/update scripts with validation
- execute_script_tool with name: Execute stored scripts by name
- list_scripts_tool: List all stored scripts
- get_script_tool: Retrieve full script details
- delete_script_tool: Delete scripts
"""

from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, KeychuteConfig
from family_assistant.storage.database import Database
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.stored_scripts import (
    delete_script_tool,
    get_script_tool,
    list_scripts_tool,
    save_script_tool,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_save_and_list_scripts(db_engine: AsyncEngine) -> None:
    """Test saving a script and verifying it appears in the list."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-1",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Save a script
    save_result = await save_script_tool(
        context,
        name="test-script-1",
        description="A simple test script",
        code="1 + 1",
    )

    # Verify save succeeded
    assert save_result.data is not None
    assert isinstance(save_result.data, dict)
    save_data = save_result.data
    assert save_data["name"] == "test-script-1"
    assert save_data["description"] == "A simple test script"

    # List scripts and verify it's there
    list_result = await list_scripts_tool(context)
    assert list_result.data is not None
    assert isinstance(list_result.data, dict)
    list_data = list_result.data
    assert list_data["count"] == 1
    assert len(list_data["scripts"]) == 1
    assert list_data["scripts"][0]["name"] == "test-script-1"
    assert list_data["scripts"][0]["description"] == "A simple test script"


@pytest.mark.asyncio
async def test_save_script_accepts_configured_keychute_api(
    db_engine: AsyncEngine,
) -> None:
    """Stored-script validation uses the configured runtime API namespace."""
    db = Database(engine=db_engine)
    processing_service = Mock()
    processing_service.tools_provider = None
    processing_service.app_config = AppConfig(
        keychute_config=KeychuteConfig(
            enabled=True,
            url="https://keychute.test",
            token="client-token",
        )
    )
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-keychute",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=processing_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    result = await save_script_tool(
        context,
        name="brokered-request",
        description="Call an API through Keychute",
        code='keychute_http_request("weather", "https://example.test")',
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["name"] == "brokered-request"


@pytest.mark.asyncio
async def test_save_and_get_script(db_engine: AsyncEngine) -> None:
    """Test saving a script and retrieving its code."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-2",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    script_code = """
x = 42
y = x * 2
y
"""

    # Save a script
    await save_script_tool(
        context,
        name="math-script",
        description="A script that does math",
        code=script_code,
    )

    # Get the script
    get_result = await get_script_tool(context, name="math-script")
    assert get_result.data is not None
    assert isinstance(get_result.data, dict)
    get_data = get_result.data
    assert get_data["name"] == "math-script"
    assert get_data["description"] == "A script that does math"
    assert get_data["script_code"] == script_code
    assert "created_at" in get_data
    assert "updated_at" in get_data


@pytest.mark.asyncio
async def test_save_and_run_script(db_engine: AsyncEngine) -> None:
    """Test saving and running a simple script."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-3",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Save a simple script
    await save_script_tool(
        context,
        name="simple-add",
        description="Add two numbers",
        code="1 + 1",
    )

    # Run the script
    run_result = await execute_script_tool(context, name="simple-add")
    assert run_result.data is not None
    assert isinstance(run_result.data, int)
    assert run_result.data == 2


@pytest.mark.asyncio
async def test_run_script_with_parameters(db_engine: AsyncEngine) -> None:
    """Test running a script with parameters passed as globals."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-4",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Save a script that uses a parameter
    parameters_schema = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "A number to multiply"},
        },
        "required": ["x"],
    }

    await save_script_tool(
        context,
        name="multiply",
        description="Multiply a number by 2",
        code="x * 2",
        parameters_schema=parameters_schema,
    )

    # Run with parameters
    run_result = await execute_script_tool(
        context, name="multiply", parameters={"x": 5}
    )
    assert run_result.data is not None
    assert isinstance(run_result.data, int)
    assert run_result.data == 10


@pytest.mark.asyncio
async def test_run_script_not_found(db_engine: AsyncEngine) -> None:
    """Test running a non-existent script returns an error."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-5",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Try to run a non-existent script
    run_result = await execute_script_tool(context, name="nonexistent")
    assert run_result.data is not None
    assert isinstance(run_result.data, dict)
    assert "error" in run_result.data
    assert "not found" in run_result.data["error"].lower()


@pytest.mark.asyncio
async def test_delete_script(db_engine: AsyncEngine) -> None:
    """Test deleting a script."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-6",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Save a script
    await save_script_tool(
        context,
        name="to-delete",
        description="Script to delete",
        code="1",
    )

    # Verify it exists
    get_result = await get_script_tool(context, name="to-delete")
    assert get_result.data is not None
    assert isinstance(get_result.data, dict)
    assert "script_code" in get_result.data

    # Delete it
    delete_result = await delete_script_tool(context, name="to-delete")
    assert delete_result.data is not None
    assert isinstance(delete_result.data, dict)
    delete_data = delete_result.data
    assert delete_data["deleted"] is True
    assert delete_data["name"] == "to-delete"

    # Verify it's gone
    get_result = await get_script_tool(context, name="to-delete")
    assert get_result.data is not None
    assert isinstance(get_result.data, dict)
    assert "error" in get_result.data


@pytest.mark.asyncio
async def test_delete_script_not_found(db_engine: AsyncEngine) -> None:
    """Test deleting a non-existent script returns an error."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-7",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Try to delete a non-existent script
    delete_result = await delete_script_tool(context, name="nonexistent")
    assert delete_result.data is not None
    assert isinstance(delete_result.data, dict)
    assert "error" in delete_result.data
    assert "not found" in delete_result.data["error"].lower()


@pytest.mark.asyncio
async def test_save_script_with_syntax_error(db_engine: AsyncEngine) -> None:
    """Test that saving a script with syntax error returns validation error."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-8",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Try to save a script with syntax error
    save_result = await save_script_tool(
        context,
        name="bad-syntax",
        description="Script with syntax error",
        code="if True\n  x = 1",  # Missing colon after if
    )

    assert save_result.data is not None
    assert isinstance(save_result.data, dict)
    assert "error" in save_result.data
    assert "validation failed" in save_result.data["error"].lower()


@pytest.mark.asyncio
async def test_update_existing_script(db_engine: AsyncEngine) -> None:
    """Test that saving a script with the same name updates it."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-9",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Save a script
    save_result1 = await save_script_tool(
        context,
        name="updatable",
        description="Original description",
        code="1 + 1",
    )
    assert isinstance(save_result1.data, dict)
    created_at1 = save_result1.data["created_at"]

    # Update the same script
    save_result2 = await save_script_tool(
        context,
        name="updatable",
        description="Updated description",
        code="2 + 2",
    )

    # Verify it was updated
    assert isinstance(save_result2.data, dict)
    assert save_result2.data["description"] == "Updated description"
    assert save_result2.data["created_at"] == created_at1
    # updated_at should be newer or same
    assert save_result2.data["updated_at"] >= save_result1.data["updated_at"]

    # Get the script and verify the code was updated
    get_result = await get_script_tool(context, name="updatable")
    assert isinstance(get_result.data, dict)
    assert get_result.data["script_code"] == "2 + 2"


@pytest.mark.asyncio
async def test_list_scripts_empty(db_engine: AsyncEngine) -> None:
    """Test listing scripts when there are none."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-10",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # List scripts (should be empty)
    list_result = await list_scripts_tool(context)
    assert list_result.data is not None
    assert isinstance(list_result.data, dict)
    list_data = list_result.data
    assert list_data["count"] == 0
    assert list_data["scripts"] == []


@pytest.mark.asyncio
async def test_save_script_with_parameters_schema(db_engine: AsyncEngine) -> None:
    """Test saving and retrieving a script with parameters schema."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-11",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "A person's name"},
            "age": {"type": "integer", "description": "Age in years"},
        },
        "required": ["name"],
    }

    # Save a script with parameters schema
    save_result = await save_script_tool(
        context,
        name="greet",
        description="Greet someone",
        code="f'Hello, {name}!'",
        parameters_schema=parameters_schema,
    )

    assert save_result.data is not None
    assert isinstance(save_result.data, dict)
    assert save_result.data["parameters_schema"] == parameters_schema

    # Verify in list
    list_result = await list_scripts_tool(context)
    assert isinstance(list_result.data, dict)
    assert list_result.data["scripts"][0]["parameters_schema"] == parameters_schema

    # Verify in get
    get_result = await get_script_tool(context, name="greet")
    assert isinstance(get_result.data, dict)
    assert get_result.data["parameters_schema"] == parameters_schema


@pytest.mark.asyncio
async def test_save_script_malformed_required_field(db_engine: AsyncEngine) -> None:
    """Malformed parameters_schema (non-string in required) is rejected at save time."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-malformed",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # required contains a non-string entry
    bad_schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": [["nested", "list"]],
    }
    save_result = await save_script_tool(
        context,
        name="bad-schema",
        description="Bad schema test",
        code="1",
        parameters_schema=bad_schema,  # type: ignore[arg-type] - deliberately malformed to test validation
    )
    assert isinstance(save_result.data, dict)
    assert "error" in save_result.data
    assert "parameters_schema" in save_result.data["error"].lower()

    # Verify it was not saved
    get_result = await get_script_tool(context, name="bad-schema")
    assert isinstance(get_result.data, dict)
    assert "error" in get_result.data


@pytest.mark.asyncio
async def test_save_script_malformed_required_not_list(db_engine: AsyncEngine) -> None:
    """parameters_schema with non-list required is rejected."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-malformed-2",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    bad_schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": "x",
    }
    save_result = await save_script_tool(
        context,
        name="bad-schema-2",
        description="Bad schema test",
        code="1",
        parameters_schema=bad_schema,  # type: ignore[arg-type] - deliberately malformed to test validation
    )
    assert isinstance(save_result.data, dict)
    assert "error" in save_result.data


@pytest.mark.asyncio
async def test_save_script_with_required_only_schema(db_engine: AsyncEngine) -> None:
    """Schema with 'required' but no 'properties' should still accept those names."""
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv-required-only",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Schema declares x as required without listing it in properties
    schema = {"type": "object", "required": ["x"]}
    save_result = await save_script_tool(
        context,
        name="required-only",
        description="Uses x without properties declaration",
        code="x * 2",
        parameters_schema=schema,
    )
    assert isinstance(save_result.data, dict)
    assert "error" not in save_result.data, f"Unexpected save error: {save_result.data}"

    # And it should run correctly with a supplied parameter
    run_result = await execute_script_tool(
        context, name="required-only", parameters={"x": 7}
    )
    assert run_result.data == 14
