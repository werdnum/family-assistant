"""Unit tests for ScriptsRepository."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[Database]:
    """Provides an entered Database for repository tests."""
    db_ctx = Database(engine=db_engine)
    yield db_ctx


class TestScriptsRepository:
    """Tests for ScriptsRepository operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_by_name(self, db_context: Database) -> None:
        """Test saving a script and retrieving it by name."""
        name = "test_script"
        description = "A test script"
        script_code = "print('Hello, World!')"

        # Save the script
        saved = await db_context.scripts.save(
            name=name,
            description=description,
            script_code=script_code,
        )

        # Verify all fields
        assert saved.id is not None
        assert saved.name == name
        assert saved.description == description
        assert saved.script_code == script_code
        assert saved.parameters_schema is None
        assert saved.created_at is not None
        assert saved.updated_at is not None

        # Retrieve by name and verify
        retrieved = await db_context.scripts.get_by_name(name)
        assert retrieved is not None
        assert retrieved.id == saved.id
        assert retrieved.name == name
        assert retrieved.description == description
        assert retrieved.script_code == script_code
        assert retrieved.parameters_schema is None

    @pytest.mark.asyncio
    async def test_save_upsert_updates_existing(self, db_context: Database) -> None:
        """Test that saving with same name updates the existing script."""
        name = "upsert_test"
        original_code = "print('original')"
        updated_code = "print('updated')"

        # Save initial script
        saved1 = await db_context.scripts.save(
            name=name,
            description="Original description",
            script_code=original_code,
        )
        original_id = saved1.id

        # Save again with same name but different content
        saved2 = await db_context.scripts.save(
            name=name,
            description="Updated description",
            script_code=updated_code,
        )

        # ID should be the same (upsert, not insert)
        assert saved2.id == original_id
        # Content should be updated
        assert saved2.description == "Updated description"
        assert saved2.script_code == updated_code
        # Updated timestamp should be newer or equal
        assert saved2.updated_at >= saved1.updated_at

    @pytest.mark.asyncio
    async def test_get_by_name_not_found(self, db_context: Database) -> None:
        """Test that get_by_name returns None for nonexistent name."""
        result = await db_context.scripts.get_by_name("nonexistent_script")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_id(self, db_context: Database) -> None:
        """Test saving a script and retrieving it by ID."""
        name = "get_by_id_test"
        description = "Test get by ID"
        script_code = "print('test')"

        # Save script
        saved = await db_context.scripts.save(
            name=name,
            description=description,
            script_code=script_code,
        )
        script_id = saved.id

        # Retrieve by ID
        retrieved = await db_context.scripts.get_by_id(script_id)
        assert retrieved is not None
        assert retrieved.id == script_id
        assert retrieved.name == name
        assert retrieved.description == description
        assert retrieved.script_code == script_code

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, db_context: Database) -> None:
        """Test that get_by_id returns None for nonexistent ID."""
        result = await db_context.scripts.get_by_id(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_list_all_empty(self, db_context: Database) -> None:
        """Test that list_all returns empty list when no scripts exist."""
        scripts = await db_context.scripts.list_all()
        assert scripts == []

    @pytest.mark.asyncio
    async def test_list_all(self, db_context: Database) -> None:
        """Test listing all scripts returns them ordered by name."""
        # Save multiple scripts
        script_names = ["charlie", "alice", "bob"]
        for name in script_names:
            await db_context.scripts.save(
                name=name,
                description=f"Description for {name}",
                script_code=f"print('{name}')",
            )

        # List all
        scripts = await db_context.scripts.list_all()

        # Should have all 3 scripts
        assert len(scripts) == 3

        # Should be ordered by name
        retrieved_names = [s.name for s in scripts]
        assert retrieved_names == ["alice", "bob", "charlie"]

    @pytest.mark.asyncio
    async def test_delete(self, db_context: Database) -> None:
        """Test deleting a script by name."""
        name = "delete_test"

        # Save script
        await db_context.scripts.save(
            name=name,
            description="Script to delete",
            script_code="print('delete me')",
        )

        # Verify it exists
        retrieved = await db_context.scripts.get_by_name(name)
        assert retrieved is not None

        # Delete it
        result = await db_context.scripts.delete(name)
        assert result is True

        # Verify it's gone
        retrieved = await db_context.scripts.get_by_name(name)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_delete_not_found(self, db_context: Database) -> None:
        """Test that deleting a nonexistent script returns False."""
        result = await db_context.scripts.delete("nonexistent_script")
        assert result is False

    @pytest.mark.asyncio
    async def test_save_with_parameters_schema(self, db_context: Database) -> None:
        """Test saving a script with a JSON schema."""
        name = "schema_test"
        schema = {
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["input"],
        }

        # Save with schema
        saved = await db_context.scripts.save(
            name=name,
            description="Script with parameters",
            script_code="print('test')",
            parameters_schema=schema,
        )

        # Verify schema is stored
        assert saved.parameters_schema == schema

        # Retrieve and verify schema is still there
        retrieved = await db_context.scripts.get_by_name(name)
        assert retrieved is not None
        assert retrieved.parameters_schema == schema

    @pytest.mark.asyncio
    async def test_save_without_parameters_schema(self, db_context: Database) -> None:
        """Test saving without schema returns None for parameters_schema."""
        name = "no_schema_test"

        # Save without schema
        saved = await db_context.scripts.save(
            name=name,
            description="Script without parameters",
            script_code="print('test')",
        )

        # Verify schema is None
        assert saved.parameters_schema is None

        # Retrieve and verify schema is still None
        retrieved = await db_context.scripts.get_by_name(name)
        assert retrieved is not None
        assert retrieved.parameters_schema is None

    @pytest.mark.asyncio
    async def test_save_with_complex_parameters_schema(
        self, db_context: Database
    ) -> None:
        """Test saving with a complex nested JSON schema."""
        name = "complex_schema_test"
        schema = {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {
                        "timeout": {"type": "number"},
                        "retries": {"type": "integer"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "enabled": {"type": "boolean"},
            },
        }

        # Save with complex schema
        saved = await db_context.scripts.save(
            name=name,
            description="Complex schema test",
            script_code="print('complex')",
            parameters_schema=schema,
        )

        # Verify schema is preserved exactly
        assert saved.parameters_schema == schema

        # Retrieve and verify
        retrieved = await db_context.scripts.get_by_name(name)
        assert retrieved is not None
        assert retrieved.parameters_schema == schema
