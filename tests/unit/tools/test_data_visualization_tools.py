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

    @pytest.mark.asyncio
    async def test_create_chart_with_data_dict(self, mock_exec_context: Mock) -> None:
        """Test creating a chart with direct data as named datasets (dict)."""
        # Create spec that references a named dataset
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Temperature chart",
            "data": {"name": "temperatures"},
            "mark": "line",
            "encoding": {
                "x": {"field": "date", "type": "temporal"},
                "y": {"field": "temp", "type": "quantitative"},
            },
        }

        # Provide data as named datasets
        data = {
            "temperatures": [
                {"date": "2024-01-01", "temp": 20},
                {"date": "2024-01-02", "temp": 22},
                {"date": "2024-01-03", "temp": 19},
            ]
        }

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=json.dumps(spec),
            data=data,
            title="Temperature Chart",
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Temperature Chart" in result.get_text()
        assert result.attachments and len(result.attachments) > 0
        assert result.attachments[0].mime_type == "image/png"

        # Verify it's a valid PNG
        content = result.attachments[0].content
        assert content is not None
        img = Image.open(io.BytesIO(content))
        assert img.format == "PNG"

    @pytest.mark.asyncio
    async def test_create_chart_with_data_list(self, mock_exec_context: Mock) -> None:
        """Test creating a chart with direct data as a list (default 'data' name)."""
        # Create spec that uses default "data" name
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Sales bar chart",
            "data": {"name": "data"},
            "mark": "bar",
            "encoding": {
                "x": {"field": "product", "type": "nominal"},
                "y": {"field": "sales", "type": "quantitative"},
            },
        }

        # Provide data as a list (will be assigned to "data" key)
        data = [
            {"product": "A", "sales": 100},
            {"product": "B", "sales": 150},
            {"product": "C", "sales": 120},
        ]

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=json.dumps(spec),
            data=data,
            title="Sales Chart",
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Sales Chart" in result.get_text()
        assert result.attachments and len(result.attachments) > 0

        # Verify it's a valid PNG
        content = result.attachments[0].content
        assert content is not None
        img = Image.open(io.BytesIO(content))
        assert img.format == "PNG"

    @pytest.mark.asyncio
    async def test_create_chart_with_data_and_attachments(
        self, mock_exec_context: Mock, mock_json_attachment: Mock
    ) -> None:
        """Test creating a chart with both data parameter and attachments."""
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Multi-dataset chart",
            "layer": [
                {
                    "data": {"name": "computed"},
                    "mark": "line",
                    "encoding": {
                        "x": {"field": "x", "type": "quantitative"},
                        "y": {"field": "y", "type": "quantitative"},
                    },
                },
                {
                    "data": {"name": "data.json"},
                    "mark": "point",
                    "encoding": {
                        "x": {"field": "month", "type": "nominal"},
                        "y": {"field": "sales", "type": "quantitative"},
                    },
                },
            ],
        }

        # Provide both computed data and attachment
        computed_data = {"computed": [{"x": 1, "y": 2}, {"x": 2, "y": 4}]}

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=json.dumps(spec),
            data=computed_data,
            data_attachments=[mock_json_attachment],
            title="Mixed Data Chart",
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Mixed Data Chart" in result.get_text()
        assert result.attachments and len(result.attachments) > 0

        # Verify attachment was accessed
        mock_json_attachment.get_content_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_chart_simulating_jq_query_composition(
        self, mock_exec_context: Mock
    ) -> None:
        """Test composition pattern: jq_query result -> create_vega_chart."""
        # Simulate what jq_query would return (list of data points)
        jq_result = [
            {"timestamp": "2024-01-01T10:00:00", "temperature": 20.5},
            {"timestamp": "2024-01-01T11:00:00", "temperature": 21.2},
            {"timestamp": "2024-01-01T12:00:00", "temperature": 22.0},
            {"timestamp": "2024-01-01T13:00:00", "temperature": 21.8},
        ]

        # Create chart spec that uses this data
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Temperature over time",
            "data": {"name": "data"},
            "mark": {"type": "line", "point": True},
            "encoding": {
                "x": {
                    "field": "timestamp",
                    "type": "temporal",
                    "title": "Time",
                },
                "y": {
                    "field": "temperature",
                    "type": "quantitative",
                    "title": "Temperature (°C)",
                },
            },
        }

        # Pass jq_query result directly to create_vega_chart
        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=json.dumps(spec),
            data=jq_result,  # Direct composition - no attachment needed
            title="Pool Temperature",
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Pool Temperature" in result.get_text()
        assert result.attachments and len(result.attachments) > 0
        assert result.attachments[0].mime_type == "image/png"

        # Verify it's a valid PNG with reasonable dimensions
        content = result.attachments[0].content
        assert content is not None
        img = Image.open(io.BytesIO(content))
        assert img.format == "PNG"
        assert img.width > 100  # Sanity check
        assert img.height > 100

    @pytest.mark.asyncio
    async def test_create_chart_with_multiple_named_datasets(
        self, mock_exec_context: Mock
    ) -> None:
        """Test creating a chart with multiple named datasets from data parameter."""
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Dual axis chart",
            "layer": [
                {
                    "data": {"name": "temperature"},
                    "mark": "line",
                    "encoding": {
                        "x": {"field": "time", "type": "temporal"},
                        "y": {
                            "field": "value",
                            "type": "quantitative",
                            "title": "Temperature",
                        },
                        "color": {"value": "red"},
                    },
                },
                {
                    "data": {"name": "humidity"},
                    "mark": "line",
                    "encoding": {
                        "x": {"field": "time", "type": "temporal"},
                        "y": {
                            "field": "value",
                            "type": "quantitative",
                            "title": "Humidity",
                        },
                        "color": {"value": "blue"},
                    },
                },
            ],
        }

        # Multiple named datasets
        data = {
            "temperature": [
                {"time": "2024-01-01T10:00", "value": 20},
                {"time": "2024-01-01T11:00", "value": 21},
            ],
            "humidity": [
                {"time": "2024-01-01T10:00", "value": 65},
                {"time": "2024-01-01T11:00", "value": 68},
            ],
        }

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=json.dumps(spec),
            data=data,
            title="Climate Data",
        )

        assert isinstance(result, ToolResult)
        assert "Created visualization: Climate Data" in result.get_text()
        assert result.attachments and len(result.attachments) > 0

        # Verify it's a valid PNG
        content = result.attachments[0].content
        assert content is not None
        img = Image.open(io.BytesIO(content))
        assert img.format == "PNG"

    @pytest.mark.asyncio
    async def test_debug_mode_simple_spec(
        self, mock_exec_context: Mock, simple_vega_lite_spec: str
    ) -> None:
        """Test debug mode returns spec as structured data instead of rendering."""
        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=simple_vega_lite_spec,
            title="Debug Test",
            debug=True,
        )

        assert isinstance(result, ToolResult)
        # Should not have PNG attachments in debug mode
        assert not result.attachments or len(result.attachments) == 0

        # Verify the returned spec is structured data
        returned_spec = result.get_data()
        assert returned_spec is not None
        assert isinstance(returned_spec, dict)
        assert "$schema" in returned_spec
        assert "data" in returned_spec

        # Verify get_text() auto-generates JSON from data
        text = result.get_text()
        assert text  # Should not be empty
        parsed = json.loads(text)  # Should be valid JSON
        assert parsed == returned_spec  # Should match the data

    @pytest.mark.asyncio
    async def test_debug_mode_with_data_attachments(
        self,
        mock_exec_context: Mock,
        vega_lite_spec_with_named_data: str,
        mock_csv_attachment: Mock,
    ) -> None:
        """Test debug mode includes merged data from attachments."""
        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=vega_lite_spec_with_named_data,
            data_attachments=[mock_csv_attachment],
            title="Debug with CSV",
            debug=True,
        )

        assert isinstance(result, ToolResult)
        assert not result.attachments or len(result.attachments) == 0

        # Get the returned spec as structured data
        returned_spec = result.get_data()
        assert returned_spec is not None
        assert isinstance(returned_spec, dict)

        # Verify data was merged into the spec
        assert "data" in returned_spec
        assert "values" in returned_spec["data"]
        # Should have the CSV data merged in
        values = returned_spec["data"]["values"]
        assert len(values) == 3
        assert values[0]["month"] == "January"
        assert values[0]["sales"] == "100"

        # Verify get_text() auto-generates JSON from data
        text = result.get_text()
        assert text  # Should not be empty
        parsed = json.loads(text)  # Should be valid JSON
        assert parsed == returned_spec  # Should match the data

    @pytest.mark.asyncio
    async def test_debug_mode_with_direct_data(self, mock_exec_context: Mock) -> None:
        """Test debug mode includes merged data from direct data parameter."""
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Test chart",
            "data": {"name": "mydata"},
            "mark": "bar",
            "encoding": {
                "x": {"field": "x", "type": "nominal"},
                "y": {"field": "y", "type": "quantitative"},
            },
        }

        data = {"mydata": [{"x": "A", "y": 10}, {"x": "B", "y": 20}]}

        result = await create_vega_chart_tool(
            mock_exec_context,
            spec=json.dumps(spec),
            data=data,
            title="Debug with Data",
            debug=True,
        )

        assert isinstance(result, ToolResult)
        assert not result.attachments or len(result.attachments) == 0

        # Get the returned spec as structured data
        returned_spec = result.get_data()
        assert returned_spec is not None
        assert isinstance(returned_spec, dict)

        # Verify data was merged into the spec
        assert "data" in returned_spec
        assert "values" in returned_spec["data"]
        assert returned_spec["data"]["values"] == data["mydata"]

        # Verify get_text() auto-generates JSON from data
        text = result.get_text()
        assert text  # Should not be empty
        parsed = json.loads(text)  # Should be valid JSON
        assert parsed == returned_spec  # Should match the data
