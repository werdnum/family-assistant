import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from family_assistant.processing import ProcessingService
from family_assistant.processing.turn_context import render_turn_context_block
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
            await processing_service.context_preparer.aggregate_context()
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
                logger.exception(
                    f"Error getting context fragments from provider '{provider.name}': {e}"
                )
                context_fragments.append({
                    "provider_name": provider.name,
                    "fragments": [],
                    "error": str(e),
                })

        # Get the system prompt template and format arguments
        system_prompt_template = processing_service.service_config.prompts.get(
            "system_prompt",
            "You are a helpful assistant.",
        )

        service_config = processing_service.service_config
        user = get_user_from_request(request)
        format_args = {
            "user_name": user.get("name") if user else "[user_name]",
            "server_url": processing_service.server_url,
            "profile_id": service_config.id,
        }

        # The time and the aggregated context no longer sit inside the system
        # prompt, so reporting only the prompt would show half of what the model
        # gets. The block below is the other half, delivered at the end of the turn.
        include_aggregated_context = service_config.include_aggregated_context
        turn_context_block = render_turn_context_block(
            current_time_str=processing_service.current_time_str(),
            aggregated_context=(
                aggregated_context if include_aggregated_context else ""
            ),
        )

        return templates.TemplateResponse(
            request,
            "context_viewer.html.j2",
            context={
                "aggregated_context": aggregated_context,
                "include_aggregated_context": include_aggregated_context,
                "turn_context_block": turn_context_block,
                "context_fragments": context_fragments,
                "system_prompt_template": system_prompt_template,
                "format_args": format_args,
                "profile_id": service_config.id,
                "total_fragments": sum(
                    len(cf["fragments"]) for cf in context_fragments
                ),
                "providers_with_errors": [
                    cf for cf in context_fragments if cf["error"]
                ],
                "user": user,
                "AUTH_ENABLED": AUTH_ENABLED,
                "now_utc": datetime.now(UTC),
            },
        )
    except Exception as e:
        logger.exception(f"Error in context viewer: {e}")
        raise HTTPException(
            status_code=500, detail=f"Error viewing context: {e!s}"
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
            candidate = processing_services_registry.get(profile_id)
            if candidate and candidate.kind == "remote":
                raise HTTPException(
                    status_code=400,
                    detail=f"Profile '{profile_id}' is a remote delegation-only profile",
                )
            if candidate:
                target_service = candidate
                logger.info(f"Using ProcessingService for profile_id: '{profile_id}'")
            else:
                logger.warning(
                    f"Profile ID '{profile_id}' not found, using default service"
                )
        else:
            logger.info("Using default processing service")

        # Get aggregated context
        aggregated_context = await target_service.context_preparer.aggregate_context()

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
                logger.exception(
                    f"Error getting context fragments from provider '{provider.name}': {e}"
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
        user_name = str((user.get("name") if user else None) or "[user_name]")

        # Rendered by the service itself rather than by a second implementation
        # here. This surface exists to report what the model is handed, and the
        # local re-render this replaces silently dropped system_prompt_docs, the
        # turn-context guidance and the profile preamble -- none of which come
        # from the template, so no amount of substituting into it can show them.
        if isinstance(target_service, ProcessingService):
            include_aggregated_context = (
                target_service.service_config.include_aggregated_context
            )
            formatted_system_prompt = target_service.format_system_prompt(
                user_name=user_name
            )
            delegation_addition = await target_service.delegation_catalog_addition()
            if delegation_addition:
                formatted_system_prompt = (
                    f"{formatted_system_prompt}\n\n{delegation_addition}".strip()
                )
            # The time and the aggregated context no longer sit inside the system
            # prompt, so reporting only the prompt would show half of what the
            # model gets. This block is the other half, delivered at the end of
            # the turn.
            turn_context_block = render_turn_context_block(
                current_time_str=target_service.current_time_str(),
                aggregated_context=(
                    aggregated_context if include_aggregated_context else ""
                ),
            )
        else:
            # A remote A2A profile builds its prompt on the far side, so there is
            # no local rendering to report. Reporting a locally invented one
            # would be exactly the divergence this endpoint exists to avoid.
            include_aggregated_context = False
            formatted_system_prompt = ""
            turn_context_block = ""

        return {
            "profile_id": target_service.service_config.id,
            "aggregated_context": aggregated_context,
            "include_aggregated_context": include_aggregated_context,
            "context_providers": context_data,
            "total_fragments": sum(cd["fragment_count"] for cd in context_data),
            "providers_with_errors": [
                cd["provider_name"] for cd in context_data if cd["error"]
            ],
            "system_prompt_template": system_prompt_template,
            "formatted_system_prompt": formatted_system_prompt,
            "turn_context_block": turn_context_block,
        }
    except Exception as e:
        logger.exception(f"Error in context API: {e}")
        raise HTTPException(
            status_code=500, detail=f"Error getting context: {e!s}"
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
            if service.kind == "remote":
                profiles.append({
                    "id": profile_id,
                    "description": service.service_config.description,
                    "llm_model": "remote",
                    "provider": "a2a",
                    "tools_count": 0,
                    "context_providers": [],
                })
                continue

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
        logger.exception(f"Error getting processing profiles: {e}")
        raise HTTPException(
            status_code=500, detail=f"Error getting profiles: {e!s}"
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
