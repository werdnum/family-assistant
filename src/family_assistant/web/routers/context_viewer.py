import logging
import re
from datetime import datetime, timezone
from string import Formatter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from family_assistant.processing import ProcessingService
from family_assistant.web.auth import AUTH_ENABLED, get_user_from_request
from family_assistant.web.dependencies import get_processing_service

logger = logging.getLogger(__name__)
context_viewer_router = APIRouter()


# Get templates from app state (will be set by app_creator.py)
def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@context_viewer_router.get("/context", response_class=HTMLResponse)
async def view_context_page(
    request: Request,
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    processing_service: Annotated[ProcessingService, Depends(get_processing_service)],
) -> HTMLResponse:
    """
    Display the context viewer page showing all context that would be provided to the LLM.
    """
    try:
        # Get aggregated context from the default processing service
        aggregated_context = (
            await processing_service._aggregate_context_from_providers()
        )

        # Get individual context fragments for detailed display
        context_fragments = []
        for provider in processing_service.context_providers:
            try:
                fragments = await provider.get_context_fragments()
                context_fragments.append({
                    "provider_name": provider.name,
                    "fragments": fragments if fragments else [],
                    "error": None,
                })
            except Exception as e:
                logger.error(
                    f"Error getting context fragments from provider '{provider.name}': {e}",
                    exc_info=True,
                )
                context_fragments.append({
                    "provider_name": provider.name,
                    "fragments": [],
                    "error": str(e),
                })

        # Get the system prompt template and format arguments
        system_prompt_template = processing_service.service_config.prompts.get(
            "system_prompt",
            "You are a helpful assistant. Current time is {current_time}.",
        )

        return templates.TemplateResponse(
            "context_viewer.html.j2",
            {
                "request": request,
                "aggregated_context": aggregated_context,
                "context_fragments": context_fragments,
                "system_prompt_template": system_prompt_template,
                "profile_id": processing_service.service_config.id,
                "total_fragments": sum(
                    len(cf["fragments"]) for cf in context_fragments
                ),
                "providers_with_errors": [
                    cf for cf in context_fragments if cf["error"]
                ],
                "user": get_user_from_request(request),
                "AUTH_ENABLED": AUTH_ENABLED,
                "now_utc": datetime.now(timezone.utc),
            },
        )
    except Exception as e:
        logger.error(f"Error in context viewer: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error viewing context: {str(e)}"
        ) from e


async def _get_context_data(
    request: Request,
    processing_service: ProcessingService,
    profile_id: str | None = None,
) -> dict:
    """
    Common implementation for context API endpoints.
    """
    try:
        # If profile_id is specified, try to get that specific processing service
        target_service = processing_service
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
        else:
            logger.info("Using default processing service")

        # Get aggregated context
        aggregated_context = await target_service._aggregate_context_from_providers()

        # Get individual context fragments
        context_data = []
        for provider in target_service.context_providers:
            try:
                fragments = await provider.get_context_fragments()
                context_data.append({
                    "provider_name": provider.name,
                    "fragments": fragments if fragments else [],
                    "error": None,
                    "fragment_count": len(fragments) if fragments else 0,
                })
            except Exception as e:
                logger.error(
                    f"Error getting context fragments from provider '{provider.name}': {e}",
                    exc_info=True,
                )
                context_data.append({
                    "provider_name": provider.name,
                    "fragments": [],
                    "error": str(e),
                    "fragment_count": 0,
                })

        # Get the system prompt template
        system_prompt_template = target_service.service_config.prompts.get(
            "system_prompt", "You are a helpful assistant."
        )

        # Get formatted system prompt with actual values
        user = get_user_from_request(request)
        user_name = user.get("name") if user else "[user_name]"

        format_args = {
            "user_name": user_name,
            "current_time": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
            "aggregated_other_context": aggregated_context,
            "server_url": target_service.server_url,
            "profile_id": target_service.service_config.id,
        }

        # Add any missing placeholders to avoid KeyErrors
        # Only match simple variable names (letters, numbers, underscores)
        placeholder_pattern = r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}"
        template_placeholders = set(
            re.findall(placeholder_pattern, system_prompt_template)
        )
        for placeholder in template_placeholders:
            if placeholder not in format_args:
                format_args[placeholder] = f"[{placeholder}]"

        # Format the system prompt safely
        try:
            # Use a safer approach that only formats valid variable placeholders
            formatter = Formatter()

            # Parse the template to find all field names
            parsed_fields = set()
            for _literal_text, field_name, _format_spec, _conversion in formatter.parse(
                system_prompt_template
            ):
                if field_name is not None:
                    parsed_fields.add(field_name)

            # Only try to format fields that are valid variable names
            safe_format_args = {}
            for field in parsed_fields:
                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", field):
                    safe_format_args[field] = format_args.get(field, f"[{field}]")

            # Format only the safe placeholders using regex for non-overlapping replacement
            def replace_placeholder(match: re.Match[str]) -> str:
                field_name = match.group(1)
                return safe_format_args.get(field_name, match.group(0))

            formatted_system_prompt = re.sub(
                r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}",
                replace_placeholder,
                system_prompt_template,
            ).strip()

        except Exception as e:
            logger.error(
                f"Error formatting system prompt: {e}, format_args: {format_args}"
            )
            formatted_system_prompt = system_prompt_template.strip()

        return {
            "profile_id": target_service.service_config.id,
            "aggregated_context": aggregated_context,
            "context_providers": context_data,
            "total_fragments": sum(cd["fragment_count"] for cd in context_data),
            "providers_with_errors": [
                cd["provider_name"] for cd in context_data if cd["error"]
            ],
            "system_prompt_template": system_prompt_template,
            "formatted_system_prompt": formatted_system_prompt,
        }
    except Exception as e:
        logger.error(f"Error in context API: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error getting context: {str(e)}"
        ) from e


@context_viewer_router.get("/api/context")
async def get_context_api(
    request: Request,
    processing_service: Annotated[ProcessingService, Depends(get_processing_service)],
    profile_id: str | None = None,
) -> dict:
    """
    API endpoint to get context data in JSON format.
    """
    return await _get_context_data(request, processing_service, profile_id)


@context_viewer_router.get("/v1/context/profiles")
async def get_processing_profiles(request: Request) -> list[dict]:
    """
    API endpoint to list all available processing profiles.
    """
    try:
        processing_services_registry = getattr(
            request.app.state, "processing_services", {}
        )

        profiles = []
        for profile_id, service in processing_services_registry.items():
            service_config = service.service_config

            profiles.append({
                "id": profile_id,
                "description": service_config.description,
                "llm_model": getattr(service.llm_client, "model", "unknown"),
                "provider": getattr(service.llm_client, "provider", "unknown"),
                "tools_count": len(await service.tools_provider.get_tool_definitions())
                if service.tools_provider
                else 0,
                "context_providers": [
                    provider.name for provider in service.context_providers
                ],
            })

        return profiles
    except Exception as e:
        logger.error(f"Error getting processing profiles: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error getting profiles: {str(e)}"
        ) from e


@context_viewer_router.get("/v1/context")
async def get_context_api_v1(
    request: Request,
    processing_service: Annotated[ProcessingService, Depends(get_processing_service)],
    profile_id: str | None = None,
) -> dict:
    """
    API v1 endpoint to get context data in JSON format.
    """
    return await _get_context_data(request, processing_service, profile_id)
