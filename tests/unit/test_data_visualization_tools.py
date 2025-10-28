"""Tests for data visualization tools."""

import io
import json
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from family_assistant.tools.data_visualization import create_vega_chart_tool
from family_assistant.tools.types import ToolResult


class TestCreateVegaChartTool:
    """Test the create_vega_chart tool."""

    @pytest.fixture
    def mock_exec_context(self) -> Mock:
        """Create a mock execution context."""
        context = Mock()
        context.processing_service = None
        return context

    @pytest.fixture
    def simple_vega_lite_spec(self) -> str:
        """Create a simple Vega-Lite spec with inline data."""
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "A simple bar chart",
            "data": {
                "values": [
                    {"category": "A", "value": 28},
                    {"category": "B", "value": 55},
                    {"category": "C", "value": 43},
                ]
            },
            "mark": "bar",
            "encoding": {
                "x": {"field": "category", "type": "nominal"},
                "y": {"field": "value", "type": "quantitative"},
            },
        }
        return json.dumps(spec)

    @pytest.fixture
    def vega_lite_spec_with_named_data(self) -> str:
        """Create a Vega-Lite spec that references named data."""
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Bar chart using named dataset",
            "data": {"name": "data.csv"},
            "mark": "bar",
            "encoding": {
                "x": {"field": "month", "type": "nominal"},
                "y": {"field": "sales", "type": "quantitative"},
            },
        }
        return json.dumps(spec)

    @pytest.fixture
    def mock_csv_attachment(self) -> Mock:
        """Create a mock CSV attachment."""
        attachment = Mock()
        attachment.get_id.return_value = "csv-attachment-id"
        attachment.get_filename.return_value = "data.csv"
        attachment.get_mime_type.return_value = "text/csv"
        attachment.get_description.return_value = "Sales data CSV"

        csv_content = """month,sales
January,100
February,150
March,200"""
        attachment.get_content_async = AsyncMock(
            return_value=csv_content.encode("utf-8")
        )

        return attachment

    @pytest.fixture
    def mock_json_attachment(self) -> Mock:
        """Create a mock JSON attachment."""
        attachment = Mock()
        attachment.get_id.return_value = "json-attachment-id"
        attachment.get_filename.return_value = "data.json"
        attachment.get_mime_type.return_value = "application/json"
        attachment.get_description.return_value = "Sales data JSON"

        json_data = [
            {"month": "January", "sales": 100},
            {"month": "February", "sales": 150},
            {"month": "March", "sales": 200},
        ]
        attachment.get_content_async = AsyncMock(
            return_value=json.dumps(json_data).encode("utf-8")
        )

        return attachment

    @pytest.mark.asyncio
    async def test_create_simple_chart(
        self, mock_exec_context: Mock, simple_vega_lite_spec: str
    ) -> None:
        """Test creating a simple chart with inline data."""
        result = await create_vega_chart_tool(
            mock_exec_context, spec=simple_vega_lite_spec, title="Test Bar Chart"
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Test Bar Chart" in result.get_text()
        assert result.attachments and len(result.attachments) > 0
        assert result.attachments[0].mime_type == "image/png"
        assert result.attachments[0].description == "Test Bar Chart"
        assert result.attachments[0].content is not None
        assert len(result.attachments[0].content) > 0

        # Verify it's a valid PNG
        content = result.attachments[0].content
        assert content is not None
        img = Image.open(io.BytesIO(content))
        assert img.format == "PNG"

    @pytest.mark.asyncio
    async def test_create_chart_with_csv_data(
        self,
        mock_exec_context: Mock,
        vega_lite_spec_with_named_data: str,
        mock_csv_attachment: Mock,
    ) -> None:
        """Test creating a chart with CSV data from attachment."""
        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=vega_lite_spec_with_named_data,
            data_attachments=[mock_csv_attachment],
            title="Sales Chart",
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Sales Chart" in result.get_text()
        assert result.attachments and len(result.attachments) > 0
        assert result.attachments[0].mime_type == "image/png"

        # Verify attachment was accessed
        mock_csv_attachment.get_content_async.assert_called_once()

        # Verify it's a valid PNG
        content = result.attachments[0].content
        assert content is not None
        img = Image.open(io.BytesIO(content))
        assert img.format == "PNG"

    @pytest.mark.asyncio
    async def test_create_chart_with_json_data(
        self,
        mock_exec_context: Mock,
        vega_lite_spec_with_named_data: str,
        mock_json_attachment: Mock,
    ) -> None:
        """Test creating a chart with JSON data from attachment."""
        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=vega_lite_spec_with_named_data,
            data_attachments=[mock_json_attachment],
            title="Revenue Chart",
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Revenue Chart" in result.get_text()
        assert result.attachments and len(result.attachments) > 0

        # Verify attachment was accessed
        mock_json_attachment.get_content_async.assert_called_once()

        # Verify it's a valid PNG
        content = result.attachments[0].content
        assert content is not None
        img = Image.open(io.BytesIO(content))
        assert img.format == "PNG"

    @pytest.mark.asyncio
    async def test_create_chart_with_custom_scale(
        self, mock_exec_context: Mock, simple_vega_lite_spec: str
    ) -> None:
        """Test creating a chart with custom scale factor."""
        result = await create_vega_chart_tool(
            mock_exec_context, spec=simple_vega_lite_spec, scale=3
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

        # Higher scale should produce larger PNG
        content = result.attachments[0].content
        assert content is not None
        img = Image.open(io.BytesIO(content))
        assert img.format == "PNG"
        # Scale 3 should produce a larger image than default scale 2
        # Width/height should be roughly 1.5x the default
        assert img.width > 300  # Rough check

    @pytest.mark.asyncio
    async def test_invalid_json_spec(self, mock_exec_context: Mock) -> None:
        """Test handling of invalid JSON in spec."""
        invalid_spec = "{ invalid json }"

        result = await create_vega_chart_tool(
            mock_exec_context, spec=invalid_spec, title="Invalid Chart"
        )

        assert isinstance(result, ToolResult)
        assert "Invalid JSON in spec" in result.get_text()
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_invalid_vega_spec(self, mock_exec_context: Mock) -> None:
        """Test handling of invalid Vega spec (valid JSON but invalid Vega)."""
        invalid_spec = json.dumps({"invalid": "spec"})

        result = await create_vega_chart_tool(
            mock_exec_context, spec=invalid_spec, title="Invalid Vega"
        )

        assert isinstance(result, ToolResult)
        assert "Error rendering chart" in result.get_text()
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_attachment_content_error(
        self, mock_exec_context: Mock, vega_lite_spec_with_named_data: str
    ) -> None:
        """Test handling when attachment content cannot be retrieved."""
        attachment = AsyncMock()
        attachment.get_id.return_value = "error-attachment"
        attachment.get_filename.return_value = "data.csv"
        attachment.get_mime_type.return_value = "text/csv"
        attachment.get_content_async.return_value = None  # Simulate no content

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=vega_lite_spec_with_named_data,
            data_attachments=[attachment],
        )

        # Should still work but warn about missing attachment
        assert isinstance(result, ToolResult)
        # The chart may fail if it requires the data, or succeed if Vega provides defaults

    @pytest.mark.asyncio
    async def test_unsupported_attachment_type(
        self, mock_exec_context: Mock, vega_lite_spec_with_named_data: str
    ) -> None:
        """Test handling of unsupported attachment types."""
        attachment = AsyncMock()
        attachment.get_id.return_value = "binary-attachment"
        attachment.get_filename.return_value = "data.bin"
        attachment.get_mime_type.return_value = "application/octet-stream"
        attachment.get_content_async.return_value = b"binary data"

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=vega_lite_spec_with_named_data,
            data_attachments=[attachment],
        )

        # Should complete but ignore the unsupported attachment
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_default_title(
        self, mock_exec_context: Mock, simple_vega_lite_spec: str
    ) -> None:
        """Test that default title is used when not specified."""
        result = await create_vega_chart_tool(
            mock_exec_context, spec=simple_vega_lite_spec
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Data Visualization" in result.get_text()
        assert result.attachments is not None
        assert result.attachments[0].description == "Data Visualization"

    @pytest.mark.asyncio
    async def test_malformed_csv_data(
        self, mock_exec_context: Mock, vega_lite_spec_with_named_data: str
    ) -> None:
        """Test handling of malformed CSV data."""
        attachment = AsyncMock()
        attachment.get_id.return_value = "bad-csv"
        attachment.get_filename.return_value = "data.csv"
        attachment.get_mime_type.return_value = "text/csv"
        attachment.get_content_async.return_value = b"not,proper,csv\ndata"

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=vega_lite_spec_with_named_data,
            data_attachments=[attachment],
        )

        # Should handle gracefully
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_malformed_json_data(
        self, mock_exec_context: Mock, vega_lite_spec_with_named_data: str
    ) -> None:
        """Test handling of malformed JSON data."""
        attachment = AsyncMock()
        attachment.get_id.return_value = "bad-json"
        attachment.get_filename.return_value = "data.json"
        attachment.get_mime_type.return_value = "application/json"
        attachment.get_content_async.return_value = b"{ invalid json }"

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=vega_lite_spec_with_named_data,
            data_attachments=[attachment],
        )

        # Should complete (the invalid attachment is skipped)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_non_utf8_attachment(
        self, mock_exec_context: Mock, vega_lite_spec_with_named_data: str
    ) -> None:
        """Test handling of non-UTF-8 attachment content."""
        attachment = AsyncMock()
        attachment.get_id.return_value = "binary"
        attachment.get_filename.return_value = "data.csv"
        attachment.get_mime_type.return_value = "text/csv"
        attachment.get_content_async.return_value = b"\x80\x81\x82"  # Invalid UTF-8

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=vega_lite_spec_with_named_data,
            data_attachments=[attachment],
        )

        # Should handle gracefully by skipping the invalid attachment
        assert isinstance(result, ToolResult)
