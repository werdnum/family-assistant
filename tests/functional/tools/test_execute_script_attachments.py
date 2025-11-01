"""Tests for execute_script tool attachment propagation."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)


@pytest.fixture
async def attachment_registry(
    tmp_path: Path,
    db_engine: AsyncEngine,
) -> AttachmentRegistry:
    """Create a real AttachmentRegistry for testing."""
    test_storage = tmp_path / "test_attachments"
    test_storage.mkdir(exist_ok=True)
    return AttachmentRegistry(
        storage_path=str(test_storage), db_engine=db_engine, config=None
    )


@pytest.mark.asyncio
async def test_execute_script_return_single_attachment(
    db_engine: AsyncEngine, attachment_registry: AttachmentRegistry
) -> None:
    """Test script returning a single attachment created by attachment_create()."""
    async with DatabaseContext(engine=db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            processing_service=None,
        )

        # Create an attachment and return it
        result = await execute_script_tool(
            ctx,
            """
attachment = attachment_create(
    content="Test data for chart",
    filename="test_data.txt",
    description="Test data"
)
attachment
""",
        )

        # Verify we got a ToolResult with an attachment
        assert isinstance(result, ToolResult)
        assert result.attachments is not None
        assert len(result.attachments) == 1
        assert result.attachments[0].mime_type == "application/octet-stream"
        assert result.attachments[0].attachment_id is not None


@pytest.mark.asyncio
async def test_execute_script_return_multiple_attachments_list(
    db_engine: AsyncEngine, attachment_registry: AttachmentRegistry
) -> None:
    """Test script returning multiple attachments in a list."""
    async with DatabaseContext(engine=db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            processing_service=None,
        )

        # Create multiple attachments and return them as a list
        result = await execute_script_tool(
            ctx,
            """
att1 = attachment_create(
    content="First chart data",
    filename="chart1.txt",
    description="First chart"
)
att2 = attachment_create(
    content="Second chart data",
    filename="chart2.txt",
    description="Second chart"
)
[att1, att2]
""",
        )

        # Verify we got a ToolResult with multiple attachments
        assert isinstance(result, ToolResult)
        assert result.attachments is not None
        assert len(result.attachments) == 2
        assert all(att.attachment_id is not None for att in result.attachments)


@pytest.mark.asyncio
async def test_execute_script_return_attachment_from_tool(
    db_engine: AsyncEngine, attachment_registry: AttachmentRegistry
) -> None:
    """Test script returning an attachment created by a tool call."""
    async with DatabaseContext(engine=db_engine) as db:
        # Create a tool that returns an attachment
        async def create_test_chart(data: str) -> ToolResult:
            # Create an attachment using the registry
            content = f"Chart for: {data}".encode()

            metadata = await attachment_registry.store_and_register_tool_attachment(
                file_content=content,
                filename="test_chart.png",
                content_type="image/png",
                tool_name="create_test_chart",
                description="Test chart",
                conversation_id="test-conv",
            )

            return ToolResult(
                text=f"Created chart for {data}",
                attachments=[
                    ToolAttachment(
                        mime_type="image/png",
                        attachment_id=metadata.attachment_id,
                    )
                ],
            )

        # Register tool
        tool_definitions = [
            {
                "type": "function",
                "function": {
                    "name": "create_test_chart",
                    "description": "Create a test chart",
                    "parameters": {
                        "type": "object",
                        "properties": {"data": {"type": "string"}},
                        "required": ["data"],
                    },
                },
            }
        ]

        local_provider = LocalToolsProvider(
            definitions=tool_definitions,
            implementations={"create_test_chart": create_test_chart},
        )
        tools_provider = CompositeToolsProvider([local_provider])

        mock_service = Mock()
        mock_service.tools_provider = tools_provider

        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            processing_service=mock_service,
        )

        # Call tool and return its result
        result = await execute_script_tool(
            ctx,
            """
chart = create_test_chart(data="temperature over time")
chart
""",
        )

        # Verify we got the attachment from the tool
        assert isinstance(result, ToolResult)
        assert result.attachments is not None
        assert len(result.attachments) == 1
        assert result.attachments[0].mime_type == "image/png"


@pytest.mark.asyncio
async def test_execute_script_functional_composition(
    db_engine: AsyncEngine, attachment_registry: AttachmentRegistry
) -> None:
    """Test functional composition: passing tool result to another tool."""
    async with DatabaseContext(engine=db_engine) as db:
        # Create tools that work together
        async def process_data(data: str) -> ToolResult:
            # Simulate data processing
            processed = data.upper()
            content = processed.encode()

            metadata = await attachment_registry.store_and_register_tool_attachment(
                file_content=content,
                filename="processed.txt",
                content_type="text/plain",
                tool_name="process_data",
                description="Processed data",
                conversation_id="test-conv",
            )

            return ToolResult(
                text=f"Processed: {processed}",
                attachments=[
                    ToolAttachment(
                        mime_type="text/plain",
                        attachment_id=metadata.attachment_id,
                    )
                ],
            )

        async def create_visualization(
            spec: str, data_attachments: list[str]
        ) -> ToolResult:
            # Simulate chart creation using data attachment
            content = f"Chart with spec: {spec} and {len(data_attachments)} data sources".encode()

            metadata = await attachment_registry.store_and_register_tool_attachment(
                file_content=content,
                filename="chart.png",
                content_type="image/png",
                tool_name="create_visualization",
                description="Visualization",
                conversation_id="test-conv",
            )

            return ToolResult(
                text="Chart created",
                attachments=[
                    ToolAttachment(
                        mime_type="image/png",
                        attachment_id=metadata.attachment_id,
                    )
                ],
            )

        # Register tools
        tool_definitions = [
            {
                "type": "function",
                "function": {
                    "name": "process_data",
                    "description": "Process data",
                    "parameters": {
                        "type": "object",
                        "properties": {"data": {"type": "string"}},
                        "required": ["data"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_visualization",
                    "description": "Create visualization",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "spec": {"type": "string"},
                            "data_attachments": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["spec", "data_attachments"],
                    },
                },
            },
        ]

        local_provider = LocalToolsProvider(
            definitions=tool_definitions,
            implementations={
                "process_data": process_data,
                "create_visualization": create_visualization,
            },
        )
        tools_provider = CompositeToolsProvider([local_provider])

        mock_service = Mock()
        mock_service.tools_provider = tools_provider

        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            processing_service=mock_service,
        )

        # Functional composition: pass result of one tool to another
        result = await execute_script_tool(
            ctx,
            """
# Process data, then visualize - functional composition
processed_data = process_data(data="raw sensor readings")
chart = create_visualization(
    spec="line chart",
    data_attachments=[processed_data]
)
chart
""",
        )

        # Verify we got the final chart attachment
        assert isinstance(result, ToolResult)
        assert result.attachments is not None
        assert len(result.attachments) == 1
        assert result.attachments[0].mime_type == "image/png"


@pytest.mark.asyncio
async def test_execute_script_mixed_attachment_sources(
    db_engine: AsyncEngine, attachment_registry: AttachmentRegistry
) -> None:
    """Test script with attachments from both attachment_create and tool calls."""
    async with DatabaseContext(engine=db_engine) as db:
        # Create a tool that returns an attachment
        async def generate_report(title: str) -> ToolResult:
            content = f"Report: {title}".encode()

            metadata = await attachment_registry.store_and_register_tool_attachment(
                file_content=content,
                filename="report.pdf",
                content_type="application/pdf",
                tool_name="generate_report",
                description=title,
                conversation_id="test-conv",
            )

            return ToolResult(
                text=f"Generated report: {title}",
                attachments=[
                    ToolAttachment(
                        mime_type="application/pdf",
                        attachment_id=metadata.attachment_id,
                    )
                ],
            )

        tool_definitions = [
            {
                "type": "function",
                "function": {
                    "name": "generate_report",
                    "description": "Generate a report",
                    "parameters": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                    },
                },
            }
        ]

        local_provider = LocalToolsProvider(
            definitions=tool_definitions,
            implementations={"generate_report": generate_report},
        )
        tools_provider = CompositeToolsProvider([local_provider])

        mock_service = Mock()
        mock_service.tools_provider = tools_provider

        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            processing_service=mock_service,
        )

        # Mix attachment_create and tool results
        result = await execute_script_tool(
            ctx,
            """
# Create a data file manually
data_file = attachment_create(
    content="Raw data: 1,2,3,4,5",
    filename="data.csv",
    description="Dataset"
)

# Generate a report using a tool
report = generate_report(title="Monthly Analysis")

# Return both
[data_file, report]
""",
        )

        # Verify we got attachments from both sources
        assert isinstance(result, ToolResult)
        assert result.attachments is not None
        assert len(result.attachments) == 2

        # Find attachments by mime type
        mime_types = {att.mime_type for att in result.attachments}
        assert "application/octet-stream" in mime_types  # from attachment_create
        assert "application/pdf" in mime_types  # from generate_report


@pytest.mark.asyncio
async def test_execute_script_nested_list_attachments(
    db_engine: AsyncEngine, attachment_registry: AttachmentRegistry
) -> None:
    """Test script returning nested structures with attachments."""
    async with DatabaseContext(engine=db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            processing_service=None,
        )

        # Test nested structures
        result = await execute_script_tool(
            ctx,
            """
att1 = attachment_create(content="Data 1", filename="d1.txt")
att2 = attachment_create(content="Data 2", filename="d2.txt")
att3 = attachment_create(content="Data 3", filename="d3.txt")

# Return nested list structure
[[att1, att2], att3]
""",
        )

        # All attachments should be flattened
        assert isinstance(result, ToolResult)
        assert result.attachments is not None
        assert len(result.attachments) == 3
