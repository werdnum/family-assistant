"""
Gemini Live API router for voice mode integration.

This router provides endpoints for:
- Generating ephemeral tokens for client-side Gemini Live API access
- Returning filtered tool definitions and system context
"""

import datetime
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from family_assistant.processing import ProcessingService
from family_assistant.web.auth import get_user_from_request
from family_assistant.web.dependencies import get_processing_service

logger = logging.getLogger(__name__)

gemini_live_router = APIRouter(prefix="/gemini", tags=["Gemini Live API"])

# Gemini Live API model for voice
# See: https://ai.google.dev/gemini-api/docs/models
GEMINI_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-09-2025"


# Type aliases for dynamic JSON schema structures.
# These must be dict[str, Any] because tool schemas are arbitrary JSON structures
# defined externally (OpenAI function calling format) with variable nested properties.
# ast-grep-ignore-block: no-dict-any - Tool schemas from external APIs (OpenAI, Gemini) have arbitrary nested JSON structure that cannot be statically typed; defining typed dicts for every possible schema variant would be impractical
ToolSchema = dict[str, Any]
GeminiToolDeclaration = dict[str, Any]
PropertySchema = dict[str, Any]
# ast-grep-ignore-end


class EphemeralTokenResponse(BaseModel):
    """Response model for ephemeral token endpoint."""

    token: str
    expires_at: str
    tools: list[GeminiToolDeclaration]
    system_instruction: str
    model: str


class EphemeralTokenRequest(BaseModel):
    """Request model for ephemeral token endpoint."""

    profile_id: str | None = None


def _convert_json_schema_type_to_gemini(schema_type: str) -> str:
    """Convert JSON Schema type to Gemini API type."""
    type_map = {
        "object": "OBJECT",
        "string": "STRING",
        "number": "NUMBER",
        "integer": "INTEGER",
        "boolean": "BOOLEAN",
        "array": "ARRAY",
    }
    return type_map.get(schema_type.lower(), schema_type.upper())


def _convert_properties_to_gemini(properties: PropertySchema) -> PropertySchema:
    """Recursively convert JSON Schema properties to Gemini format."""
    result: PropertySchema = {}
    for name, prop_schema in properties.items():
        converted: PropertySchema = {
            "type": _convert_json_schema_type_to_gemini(
                prop_schema.get("type", "string")
            )
        }
        if "description" in prop_schema:
            converted["description"] = prop_schema["description"]
        if "properties" in prop_schema:
            converted["properties"] = _convert_properties_to_gemini(
                prop_schema["properties"]
            )
        if "items" in prop_schema:
            items = prop_schema["items"]
            converted["items"] = {
                "type": _convert_json_schema_type_to_gemini(items.get("type", "string"))
            }
        if "enum" in prop_schema:
            converted["enum"] = prop_schema["enum"]
        result[name] = converted
    return result


def _convert_tool_to_gemini_format(tool: ToolSchema) -> GeminiToolDeclaration:
    """Convert a single tool from OpenAI function format to Gemini FunctionDeclaration format."""
    func = tool.get("function", tool)
    gemini_func: GeminiToolDeclaration = {
        "name": func["name"],
        "description": func.get("description", ""),
    }

    if "parameters" in func:
        params = func["parameters"]
        gemini_params: PropertySchema = {
            "type": _convert_json_schema_type_to_gemini(params.get("type", "object")),
        }
        if "properties" in params:
            gemini_params["properties"] = _convert_properties_to_gemini(
                params["properties"]
            )
        if "required" in params:
            gemini_params["required"] = params["required"]
        gemini_func["parameters"] = gemini_params

    return gemini_func


def convert_tools_to_gemini_format(
    openai_tools: list[ToolSchema], tools_to_exclude: set[str]
) -> list[GeminiToolDeclaration]:
    """
    Convert OpenAI function format tools to Gemini FunctionDeclaration format.

    Also filters out tools that require confirmation (not suitable for voice mode).

    Args:
        openai_tools: List of tools in OpenAI function calling format
        tools_to_exclude: Set of tool names to exclude (e.g., tools requiring confirmation)

    Returns:
        List containing a single dict with functionDeclarations key
    """
    gemini_functions = []
    for tool in openai_tools:
        func = tool.get("function", tool)
        tool_name = func.get("name", "")

        # Skip tools that require confirmation
        if tool_name in tools_to_exclude:
            logger.debug(
                f"Excluding tool '{tool_name}' from voice mode (requires confirmation)"
            )
            continue

        gemini_functions.append(_convert_tool_to_gemini_format(tool))

    if not gemini_functions:
        return []

    return [{"functionDeclarations": gemini_functions}]


async def _get_formatted_system_prompt(
    request: Request,
    processing_service: ProcessingService,
) -> str:
    """Get the formatted system prompt with context injected."""
    try:
        # Get aggregated context from providers
        aggregated_context = (
            await processing_service._aggregate_context_from_providers()
        )

        # Get system prompt template
        system_prompt_template = processing_service.service_config.prompts.get(
            "system_prompt", "You are a helpful assistant."
        )

        # Get user info
        user = get_user_from_request(request)
        user_name = user.get("name") if user else "User"

        # Format the system prompt
        now = datetime.datetime.now(datetime.UTC)
        format_args = {
            "user_name": user_name,
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "aggregated_other_context": aggregated_context,
            "server_url": processing_service.server_url,
            "profile_id": processing_service.service_config.id,
        }

        # Simple placeholder replacement
        formatted = system_prompt_template
        for key, value in format_args.items():
            formatted = formatted.replace(f"{{{key}}}", str(value))

        # Add voice mode specific instruction
        voice_instruction = (
            "\n\n[Voice Mode Active] You are currently in voice conversation mode. "
            "Keep responses concise and conversational. Speak naturally as if talking to the user."
        )

        return formatted.strip() + voice_instruction

    except Exception as e:
        logger.error(f"Error getting system prompt: {e}", exc_info=True)
        return "You are a helpful voice assistant. Keep responses concise and conversational."


@gemini_live_router.post("/ephemeral-token")
async def create_ephemeral_token(
    request: Request,
    payload: EphemeralTokenRequest,
    processing_service: Annotated[ProcessingService, Depends(get_processing_service)],
) -> EphemeralTokenResponse:
    """
    Generate an ephemeral token for Gemini Live API access.

    This endpoint creates a short-lived token that can be safely used
    client-side for direct WebSocket connections to Gemini Live API.

    The token is valid for:
    - 1 minute to initiate a new session
    - 30 minutes for sending data once a session is established

    Returns filtered tools (excluding those requiring confirmation) and
    the formatted system prompt with context.
    """
    # Get API key from environment
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable not set")
        raise HTTPException(
            status_code=500,
            detail="Voice mode is not configured. GEMINI_API_KEY is required.",
        )

    # Get the target processing service (handle profile_id)
    target_service = processing_service
    profile_id = payload.profile_id
    if profile_id:
        processing_services_registry = getattr(
            request.app.state, "processing_services", {}
        )
        if profile_id in processing_services_registry:
            target_service = processing_services_registry[profile_id]
            logger.info(f"Using ProcessingService for profile_id: '{profile_id}'")
        else:
            logger.warning(
                f"Profile ID '{profile_id}' not found, using default service"
            )

    # Get tool definitions from app state
    tool_definitions = getattr(request.app.state, "tool_definitions", [])

    # Get tools requiring confirmation from the profile config
    # These will be excluded from voice mode
    profile_config = target_service.service_config
    tools_config = getattr(profile_config, "tools", {})
    confirm_tools: set[str] = set()

    if isinstance(tools_config, dict):
        confirm_tools = set(tools_config.get("confirm_tools", []))
    elif hasattr(tools_config, "confirm_tools"):
        confirm_tools = set(getattr(tools_config, "confirm_tools", []))

    logger.info(f"Tools requiring confirmation (excluded from voice): {confirm_tools}")

    # Convert tools to Gemini format, filtering out confirmation-required tools
    gemini_tools = convert_tools_to_gemini_format(tool_definitions, confirm_tools)
    tool_count = len(gemini_tools[0]["functionDeclarations"]) if gemini_tools else 0
    logger.info(f"Converted {tool_count} tools to Gemini format for voice mode")

    # Get formatted system prompt with context
    system_instruction = await _get_formatted_system_prompt(request, target_service)

    # Create ephemeral token via Google GenAI SDK
    try:
        from google import (  # noqa: PLC0415 - Import here to handle missing dependency gracefully
            genai,
        )

        client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})

        now = datetime.datetime.now(tz=datetime.UTC)
        token_response = client.auth_tokens.create(
            config={
                "uses": 1,  # Single use token
                "expire_time": now + datetime.timedelta(minutes=30),
                "new_session_expire_time": now + datetime.timedelta(minutes=1),
            }
        )

        expires_at = (now + datetime.timedelta(minutes=30)).isoformat()

        logger.info("Successfully created Gemini Live ephemeral token")

        return EphemeralTokenResponse(
            token=token_response.name,
            expires_at=expires_at,
            tools=gemini_tools,
            system_instruction=system_instruction,
            model=GEMINI_LIVE_MODEL,
        )

    except ImportError as e:
        logger.error(f"google-genai SDK not available: {e}")
        raise HTTPException(
            status_code=500,
            detail="Voice mode dependencies not installed. Please install google-genai.",
        ) from e
    except Exception as e:
        logger.error(f"Error creating ephemeral token: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create voice session: {e!s}",
        ) from e


class GeminiLiveStatus(BaseModel):
    """Response model for status endpoint."""

    available: bool
    model: str
    features: dict[str, bool]
    limits: dict[str, int]


@gemini_live_router.get("/status")
async def get_gemini_live_status() -> GeminiLiveStatus:
    """Check if Gemini Live API is available and configured."""
    api_key = os.environ.get("GEMINI_API_KEY")
    return GeminiLiveStatus(
        available=bool(api_key),
        model=GEMINI_LIVE_MODEL,
        features={
            "voice_input": True,
            "voice_output": True,
            "function_calling": True,
            "transcription": True,
        },
        limits={
            "session_duration_minutes": 15,
            "token_validity_minutes": 30,
        },
    )
