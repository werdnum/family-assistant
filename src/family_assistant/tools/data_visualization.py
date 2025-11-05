"""
Data visualization tools.

This module provides tools for creating data visualizations from Vega and Vega-Lite
specifications and returning them as PNG attachments.
"""

import asyncio
import csv
import io
import json
import logging
from typing import TYPE_CHECKING, Any

import vl_convert as vlc

from family_assistant.tools.types import ToolAttachment, ToolResult

if TYPE_CHECKING:
    from family_assistant.scripting.apis.attachments import ScriptAttachment
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Tool Definitions
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
DATA_VISUALIZATION_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_vega_chart",
            "description": "Create a data visualization from a Vega or Vega-Lite specification and return it as a PNG image. The LLM provides the spec and optional data attachments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": "Vega or Vega-Lite specification as a JSON string. Use Vega-Lite for simpler charts (recommended). The spec should include the data inline or use named datasets that will be populated from data_attachments.",
                    },
                    "data_attachments": {
                        "type": "array",
                        "items": {"type": "attachment"},
                        "description": "Optional list of attachment IDs containing CSV or JSON data files to use in the visualization. Each attachment should be referenced by name in the spec's data field.",
                        "default": [],
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title for the chart that will be shown to the user",
                        "default": "Data Visualization",
                    },
                    "scale": {
                        "type": "number",
                        "description": "Scale factor for the output PNG (default 2 for high DPI displays)",
                        "default": 2,
                    },
                },
                "required": ["spec"],
            },
        },
    },
]


async def create_vega_chart_tool(
    exec_context: "ToolExecutionContext",
    spec: str,
    data_attachments: list["ScriptAttachment"] | None = None,
    title: str = "Data Visualization",
    scale: float = 2,
) -> ToolResult:
    """
    Create a data visualization from a Vega or Vega-Lite specification.

    Args:
        exec_context: The execution context
        spec: Vega or Vega-Lite specification as a JSON string
        data_attachments: Optional list of attachments containing data files (CSV/JSON)
        title: Title for the chart
        scale: Scale factor for PNG output (default 2 for high DPI)

    Returns:
        ToolResult with PNG attachment of the rendered chart
    """
    logger.info(f"Creating Vega chart: {title}")

    try:
        # Parse the spec
        try:
            spec_dict = json.loads(spec)
        except json.JSONDecodeError as e:
            return ToolResult(text=f"Invalid JSON in spec: {str(e)}")

        # Process data attachments if provided
        if data_attachments:
            data_dict = {}
            for attachment in data_attachments:
                # Get attachment content
                content_bytes = await attachment.get_content_async()
                if not content_bytes:
                    logger.warning(
                        f"Could not retrieve content for attachment {attachment.get_id()}"
                    )
                    continue

                # Decode content
                try:
                    content_str = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning(
                        f"Attachment {attachment.get_id()} is not valid UTF-8 text"
                    )
                    continue

                # Parse based on MIME type
                mime_type = attachment.get_mime_type()
                attachment_filename = attachment.get_filename() or attachment.get_id()

                if mime_type == "application/json" or attachment_filename.endswith(
                    ".json"
                ):
                    try:
                        data_dict[attachment_filename] = json.loads(content_str)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Attachment {attachment.get_id()} is not valid JSON"
                        )
                        continue
                elif mime_type == "text/csv" or attachment_filename.endswith(".csv"):
                    # For CSV, we'll parse it into a list of dicts
                    # Use asyncio.to_thread to avoid blocking on large CSV files
                    # ast-grep-ignore: no-dict-any - CSV parsing returns dynamic row types
                    def _parse_csv(csv_content: str) -> list[dict[str, Any]]:
                        reader = csv.DictReader(io.StringIO(csv_content))
                        return list(reader)

                    data_dict[attachment_filename] = await asyncio.to_thread(
                        _parse_csv, content_str
                    )
                else:
                    logger.warning(
                        f"Unsupported attachment type: {mime_type} for {attachment.get_id()}"
                    )
                    continue

            # Merge data into spec if we have any
            if data_dict:
                # Handle both Vega and Vega-Lite formats
                if "data" in spec_dict:
                    # If data is a dict with a "name" field, replace values
                    if (
                        isinstance(spec_dict["data"], dict)
                        and "name" in spec_dict["data"]
                    ):
                        data_name = spec_dict["data"]["name"]
                        if data_name in data_dict:
                            spec_dict["data"]["values"] = data_dict[data_name]
                    # If data is a list (Vega format), look for named datasets
                    elif isinstance(spec_dict["data"], list):
                        for data_item in spec_dict["data"]:
                            if "name" in data_item and data_item["name"] in data_dict:
                                data_item["values"] = data_dict[data_item["name"]]

                # Also check for datasets (Vega-Lite format)
                if "datasets" in spec_dict:
                    spec_dict["datasets"].update(data_dict)

        # Determine if this is Vega or Vega-Lite based on schema
        is_vega_lite = False
        if "$schema" in spec_dict:
            is_vega_lite = "vega-lite" in spec_dict["$schema"].lower()

        # Convert to PNG using vl-convert
        # Use asyncio.to_thread as vl-convert rendering is CPU-intensive and can block for seconds
        try:

            def _render_chart() -> bytes:
                if is_vega_lite:
                    return vlc.vegalite_to_png(
                        vl_spec=spec_dict,
                        scale=scale,
                    )
                else:
                    return vlc.vega_to_png(
                        vg_spec=spec_dict,
                        scale=scale,
                    )

            png_data = await asyncio.to_thread(_render_chart)
        except Exception as e:
            logger.error(f"Error rendering Vega spec: {e}", exc_info=True)
            return ToolResult(text=f"Error rendering chart: {str(e)}")

        # Create attachment
        attachment = ToolAttachment(
            content=png_data,
            mime_type="image/png",
            description=title,
        )

        return ToolResult(
            text=f"Created visualization: {title}", attachments=[attachment]
        )

    except Exception as e:
        logger.error(f"Error creating Vega chart: {e}", exc_info=True)
        return ToolResult(text=f"Error creating chart: {str(e)}")
