"""Tests for Home Assistant event source validation."""

from unittest.mock import Mock

import pytest

from family_assistant.events.home_assistant_source import HomeAssistantSource


class TestHomeAssistantValidation:
    """Test Home Assistant event source validation."""

    @pytest.fixture
    def mock_client(self) -> Mock:
        """Create a mock Home Assistant client."""
        client = Mock()
        # Mock get_states to return some test entities
        mock_states = [
            Mock(entity_id="person.alex_smith"),
            Mock(entity_id="person.taylor_smith"),
            Mock(entity_id="light.living_room"),
            Mock(entity_id="light.bedroom"),
            Mock(entity_id="switch.garage"),
            Mock(entity_id="sensor.temperature"),
            Mock(entity_id="binary_sensor.motion_detected"),
        ]
        client.get_states = Mock(return_value=mock_states)
        return client

    @pytest.fixture
    def ha_source(self, mock_client: Mock) -> HomeAssistantSource:
        """Create a Home Assistant source with mock client."""
        return HomeAssistantSource(mock_client)

    @pytest.mark.asyncio
    async def test_valid_entity_id(self, ha_source: HomeAssistantSource) -> None:
        """Test validation with a valid entity ID that exists."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "person.alex_smith"
        })
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_shortened_person_entity(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test validation catches common mistake of shortened person entity."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "person.alex"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.field == "entity_id"
        assert "not found" in error.error
        assert error.suggestion == "Did you mean 'person.alex_smith'?"
        assert error.similar_values is not None
        assert "person.alex_smith" in error.similar_values

    @pytest.mark.asyncio
    async def test_invalid_entity_format(self, ha_source: HomeAssistantSource) -> None:
        """Test validation catches invalid entity ID format."""
        test_cases = [
            ("invalid-entity", "format"),  # dash instead of dot
            ("invalid_entity", "format"),  # no domain
            ("invalid entity", "format"),  # space
            (".entity", "format"),  # no domain
            ("domain.", "format"),  # no object_id
            ("domain..entity", "format"),  # double dot
        ]

        for entity_id, error_type in test_cases:
            result = await ha_source.validate_match_conditions({"entity_id": entity_id})
            assert result.valid is False
            assert len(result.errors) == 1
            error = result.errors[0]
            assert error.field == "entity_id"
            if error_type == "format":
                assert "Invalid entity ID format" in error.error
                assert error.suggestion is not None
                assert (
                    "person.alex_smith" in error.suggestion
                    or "domain.object_id" in error.error
                )

    @pytest.mark.asyncio
    async def test_uppercase_entity_rejected_by_api(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test that uppercase entities pass format check but fail API check."""
        # Since regex is permissive, uppercase passes format but fails API
        result = await ha_source.validate_match_conditions({
            "entity_id": "Invalid.Entity"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.field == "entity_id"
        assert "not found" in error.error  # API check, not format check

    @pytest.mark.asyncio
    async def test_non_string_entity_id(self, ha_source: HomeAssistantSource) -> None:
        """Test validation catches non-string entity IDs."""
        test_cases = [
            123,
            12.34,
            True,
            None,
            ["person.alex"],
            {"entity": "person.alex"},
        ]

        for entity_id in test_cases:
            result = await ha_source.validate_match_conditions({"entity_id": entity_id})
            assert result.valid is False
            assert len(result.errors) == 1
            error = result.errors[0]
            assert error.field == "entity_id"
            assert "must be a string" in error.error
            assert type(entity_id).__name__ in error.error

    @pytest.mark.asyncio
    async def test_nonexistent_entity(self, ha_source: HomeAssistantSource) -> None:
        """Test validation catches entity that doesn't exist."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "person.unknown"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.field == "entity_id"
        assert "not found" in error.error
        assert error.similar_values is not None
        # Should show other person entities as suggestions
        assert "person.alex_smith" in error.similar_values
        assert "person.taylor_smith" in error.similar_values

    @pytest.mark.asyncio
    async def test_no_entity_id_field(self, ha_source: HomeAssistantSource) -> None:
        """Test validation passes when no entity_id field is present."""
        result = await ha_source.validate_match_conditions({
            "some_other_field": "value",
            "another_field": 123,
        })
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_api_error_becomes_warning(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test that API errors become warnings, not validation failures."""
        # Make get_states raise an exception
        ha_source.client.get_states.side_effect = Exception("API connection failed")

        result = await ha_source.validate_match_conditions({
            "entity_id": "person.alex_smith"
        })
        # Should still be valid since we can't verify
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 1
        assert "Could not verify entity existence" in result.warnings[0]

    @pytest.mark.asyncio
    async def test_multiple_match_conditions(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test validation with multiple match conditions."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "light.living_room",
            "event_type": "state_changed",
            "some_other_condition": "value",
        })
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_taylor_entity_suggestion(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test validation provides suggestion for Taylor entity."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "person.taylor"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.suggestion == "Did you mean 'person.taylor_smith'?"

    @pytest.mark.asyncio
    async def test_similar_values_limit(self, ha_source: HomeAssistantSource) -> None:
        """Test that similar values are limited to 5."""
        # Add many light entities
        ha_source.client.get_states.return_value.extend([
            Mock(entity_id=f"light.room_{i}") for i in range(10)
        ])

        result = await ha_source.validate_match_conditions({
            "entity_id": "light.nonexistent"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.similar_values is not None
        assert len(error.similar_values) == 5  # Should be limited to 5
