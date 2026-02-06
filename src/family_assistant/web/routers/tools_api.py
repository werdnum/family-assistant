import contextlib
import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    ToolExecutionContext,
    ToolNotFoundError,
    ToolsProvider,
)
from family_assistant.tools.types import ToolResult
from family_assistant.web.dependencies import (
    get_db,
    get_tools_provider_dependency,
)

logger = logging.getLogger(__name__)
tools_api_router = APIRouter()


# --- Pydantic model for Tool Execution API ---
class ToolExecutionRequest(BaseModel):
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    arguments: dict[str, Any]


@tools_api_router.post("/execute/{tool_name}", response_class=JSONResponse)
async def execute_tool_api(
    tool_name: str,
    request: Request,  # Keep request for potential context later
    payload: ToolExecutionRequest,
    tools_provider: Annotated[ToolsProvider, Depends(get_tools_provider_dependency)],
    db_context: Annotated[
        DatabaseContext, Depends(get_db)
    ],  # Inject DB context if tools need it
) -> JSONResponse:
    """Executes a specified tool with the given arguments."""
    logger.info(
        f"Received execution request for tool: {tool_name} with args: {payload.arguments}"
    )

    # --- Retrieve necessary config and services from app state ---
    app_config = getattr(request.app.state, "config", None)
    if not app_config:
        logger.error("Main application configuration not found in app state.")
        timezone_str = "UTC"
    else:
        # Get default timezone from default profile settings
        timezone_str = app_config.default_profile_settings.processing_config.timezone

    # Get infrastructure dependencies from app state
    processing_service = getattr(request.app.state, "processing_service", None)
    clock = getattr(request.app.state, "clock", None)
    event_sources = getattr(request.app.state, "event_sources", None)
    attachment_registry = getattr(request.app.state, "attachment_registry", None)
    root_tools_provider = getattr(request.app.state, "tools_provider", None)

    # Find camera backend from any profile that has one configured
    camera_backend = None
    processing_services = getattr(request.app.state, "processing_services", {})
    for service in processing_services.values():
        if hasattr(service, "camera_backend") and service.camera_backend is not None:
            camera_backend = service.camera_backend
            break

    # --- Create Execution Context ---
    # We need some context, minimum placeholders for now
    # Generate a unique ID for this specific API call context
    # This isn't a persistent conversation like Telegram

    execution_context = ToolExecutionContext(
        interface_type="api",  # Identify interface
        conversation_id=str(uuid.uuid4()),
        user_name="APIUser",  # Added
        turn_id=str(uuid.uuid4()),
        db_context=db_context,
        # Infrastructure fields (required - no defaults)
        processing_service=processing_service,
        clock=clock,
        home_assistant_client=processing_service.home_assistant_client
        if processing_service
        else None,
        event_sources=event_sources,
        attachment_registry=attachment_registry,
        camera_backend=camera_backend,
        # Optional fields (with defaults)
        chat_interface=None,  # No direct chat interface for API calls
        timezone_str=timezone_str,  # Pass fetched timezone string
        request_confirmation_callback=None,  # No confirmation from API for now
        tools_provider=root_tools_provider,  # Pass root tools provider for execute_script
    )

    try:
        result = await tools_provider.execute_tool(
            name=tool_name, arguments=payload.arguments, context=execution_context
        )
        logger.info(f"Tool '{tool_name}' executed successfully.")

        # Convert ToolResult to serializable format
        final_result: Any
        if isinstance(result, ToolResult):
            final_result = {}
            if result.text is not None:
                final_result["text"] = result.text
            if result.data is not None:
                final_result["data"] = result.data
            if result.attachments:
                # Include attachment metadata but not binary content
                final_result["attachments"] = [
                    {
                        "mime_type": att.mime_type,
                        "description": att.description,
                        "has_content": att.content is not None,
                        "content_length": len(att.content) if att.content else 0,
                    }
                    for att in result.attachments
                ]
        elif isinstance(result, str):
            # Attempt to parse result if it's a JSON string
            final_result = result
            with contextlib.suppress(json.JSONDecodeError):
                final_result = json.loads(result)
        else:
            final_result = result

        return JSONResponse(
            content={"success": True, "result": final_result}, status_code=200
        )
    except ToolNotFoundError:
        logger.warning(f"Tool '{tool_name}' not found for execution request.")
        raise HTTPException(
            status_code=404, detail=f"Tool '{tool_name}' not found."
        ) from None
    except (
        ValidationError
    ) as ve:  # Catch Pydantic validation errors if execute_tool raises them
        logger.warning(f"Argument validation error for tool '{tool_name}': {ve}")
        raise HTTPException(
            status_code=400, detail=f"Invalid arguments for tool '{tool_name}': {ve}"
        ) from ve
    except (
        TypeError
    ) as te:  # Catch potential argument mismatches within the tool function
        logger.error(
            f"Type error during execution of tool '{tool_name}': {te}", exc_info=True
        )
        raise HTTPException(
            status_code=400,
            detail=f"Argument mismatch or type error in tool '{tool_name}': {te}",
        ) from te
    except Exception as e:
        logger.error(f"Error executing tool '{tool_name}': {e}", exc_info=True)
        # Avoid leaking internal error details unless intended
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred while executing tool '{tool_name}'.",
        ) from e


@tools_api_router.get("/definitions")
async def get_tool_definitions(
    request: Request,
    tools_provider: Annotated[ToolsProvider, Depends(get_tools_provider_dependency)],
) -> JSONResponse:
    """Development endpoint to list all available tools with their schemas."""
    try:
        # Use the tools provider to get the latest definitions, including MCP tools
        tool_definitions = await tools_provider.get_tool_definitions()
    except Exception as e:
        logger.error(f"Error fetching tool definitions from provider: {e}")
        # Fallback to app state if provider fails
        tool_definitions = getattr(request.app.state, "tool_definitions", [])

    return JSONResponse(
        content={"tools": tool_definitions, "count": len(tool_definitions)}
    )
