import json
import logging
from datetime import datetime, timezone  # Added

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from family_assistant.tools.schema import render_schema_as_html
from family_assistant.web.auth import AUTH_ENABLED

logger = logging.getLogger(__name__)
tools_ui_router = APIRouter()

# Simple in-memory cache for rendered tool schema HTML, keyed by tool name
_tool_html_cache: dict[str, str] = {}


@tools_ui_router.get("/tools", response_class=HTMLResponse, name="ui_list_tools")
async def view_tools(request: Request) -> HTMLResponse:
    """Serves the page displaying available tools."""
    global _tool_html_cache
    templates = request.app.state.templates
    try:
        tool_definitions = getattr(request.app.state, "tool_definitions", [])
        if not tool_definitions:
            logger.warning("No tool definitions found in app state for /tools page.")
        # Generate HTML for each tool's parameters on demand, using cache
        rendered_tools = []
        for tool in tool_definitions:
            tool_copy = tool.copy()  # Avoid modifying the original dict in state
            tool_name = tool_copy.get("function", {}).get("name", "UnknownTool")

            # Check cache first
            if tool_name in _tool_html_cache:
                tool_copy["parameters_html"] = _tool_html_cache[tool_name]
            else:
                schema_dict = tool_copy.get("function", {}).get("parameters")
                # Serialize the schema dict to a stable JSON string for the rendering function
                schema_json_str = (
                    json.dumps(schema_dict, sort_keys=True) if schema_dict else None
                )
                # Call the rendering function
                generated_html = render_schema_as_html(schema_json_str)
                tool_copy["parameters_html"] = generated_html
                _tool_html_cache[tool_name] = generated_html  # Store in cache

            rendered_tools.append(tool_copy)
        return templates.TemplateResponse(
            "tools.html",
            {
                "request": request,
                "tools": rendered_tools,
                "user": request.session.get("user"),
                "AUTH_ENABLED": AUTH_ENABLED,  # Pass to base template
                "now_utc": datetime.now(timezone.utc),  # Pass to base template
            },
        )
    except Exception as e:
        logger.error(f"Error fetching tool definitions: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to fetch tool definitions"
        ) from e
