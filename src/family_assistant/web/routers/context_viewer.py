import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from family_assistant.processing import ProcessingService
from family_assistant.web.auth import AUTH_ENABLED
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

        # Get available format arguments (similar to what's done in processing.py)
        format_args = {
            "user_name": "[user_name]",
            "current_time": "[current_time]",
            "aggregated_other_context": aggregated_context,
            "server_url": processing_service.server_url,
            "profile_id": processing_service.service_config.id,
        }

        return templates.TemplateResponse(
            "context_viewer.html.j2",
            {
                "request": request,
                "aggregated_context": aggregated_context,
                "context_fragments": context_fragments,
                "system_prompt_template": system_prompt_template,
                "format_args": format_args,
                "profile_id": processing_service.service_config.id,
                "total_fragments": sum(
                    len(cf["fragments"]) for cf in context_fragments
                ),
                "providers_with_errors": [
                    cf for cf in context_fragments if cf["error"]
                ],
                "user": request.session.get("user"),
                "AUTH_ENABLED": AUTH_ENABLED,
                "now_utc": datetime.now(timezone.utc),
            },
        )
    except Exception as e:
        logger.error(f"Error in context viewer: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error viewing context: {str(e)}"
        ) from e


@context_viewer_router.get("/api/context")
async def get_context_api(
    processing_service: Annotated[ProcessingService, Depends(get_processing_service)],
    profile_id: str | None = None,
) -> dict:
    """
    API endpoint to get context data in JSON format.
    """
    try:
        # If profile_id is specified, try to get that specific processing service
        target_service = processing_service
        if profile_id:
            # Access the processing services registry from app state
            # This would need to be injected or accessed differently in a real implementation
            logger.info(
                f"Profile ID '{profile_id}' requested, using default service for now"
            )

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

        return {
            "profile_id": target_service.service_config.id,
            "aggregated_context": aggregated_context,
            "context_providers": context_data,
            "total_fragments": sum(cd["fragment_count"] for cd in context_data),
            "providers_with_errors": [
                cd["provider_name"] for cd in context_data if cd["error"]
            ],
            "system_prompt_template": target_service.service_config.prompts.get(
                "system_prompt", "You are a helpful assistant."
            ),
        }
    except Exception as e:
        logger.error(f"Error in context API: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error getting context: {str(e)}"
        ) from e
