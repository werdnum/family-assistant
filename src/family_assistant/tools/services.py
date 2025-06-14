"""Service delegation tools.

This module contains tools for delegating requests to other specialized
assistant profiles (services).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


logger = logging.getLogger(__name__)

# Type alias for the confirmation callback
ConfirmationCallbackSignature = Callable[
    [str, str, str | None, str, str, dict[str, Any], float],
    asyncio.Future[bool],
]

# Tool Definitions
SERVICE_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "delegate_to_service",
            "description": (
                "Delegates a specific user request to another specialized assistant profile (service) "
                "that might have different tools or capabilities. Use this if the main assistant "
                "cannot handle a request directly or if a specialized profile is more appropriate "
                "for the task. The target profile's 'delegation_security_level' (blocked, confirm, "
                "unrestricted) can override the 'confirm_delegation' parameter.\n\n"
                "Available service profiles:\n{available_service_profiles_with_descriptions}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_service_id": {
                        "type": "string",
                        "description": "The unique ID of the target service profile to delegate the request to (e.g., 'browser_profile').",
                    },
                    "user_request": {
                        "type": "string",
                        "description": "The specific request, question, or prompt to be processed by the target service.",
                    },
                    "confirm_delegation": {
                        "type": "boolean",
                        "description": "Optional. If true, explicitly ask the user for confirmation before delegating the task. Defaults to false. This may be overridden if the target profile's security level is 'confirm' (always confirm) or 'blocked' (never delegate). If the 'delegate_to_service' tool itself is configured to require confirmation for the current profile, user confirmation will also be sought.",
                        "default": False,
                    },
                },
                "required": ["target_service_id", "user_request"],
            },
        },
    },
]


# Tool Implementations
async def delegate_to_service_tool(
    exec_context: ToolExecutionContext,
    target_service_id: str,
    user_request: str,
    confirm_delegation: bool = False,
) -> str:
    """
    Delegates a user request to another specialized assistant profile (service).
    """
    from family_assistant.telegram_bot import telegramify_markdown

    logger.info(
        f"Executing delegate_to_service_tool: target='{target_service_id}', request='{user_request[:50]}...', confirm={confirm_delegation}"
    )

    if (
        not exec_context.processing_service
        or not exec_context.processing_service.processing_services_registry
    ):
        logger.error(
            "Processing services registry not available in the current execution context."
        )
        return "Error: Service registry is not available to delegate the task."

    registry = exec_context.processing_service.processing_services_registry
    target_service = registry.get(target_service_id)

    if not target_service:
        logger.error(
            f"Target service profile ID '{target_service_id}' not found in the registry."
        )
        return f"Error: Target service profile '{target_service_id}' not found."

    # Check target service's delegation security level
    target_security_level = getattr(
        target_service.service_config, "delegation_security_level", "confirm"
    )  # Default to 'confirm' if not set

    if target_security_level == "blocked":
        logger.warning(
            f"Delegation to service '{target_service_id}' is blocked by its security policy."
        )
        return f"Error: Delegation to service profile '{target_service_id}' is not allowed."

    # Determine if confirmation is needed based on target's policy and tool's argument
    needs_confirmation_due_to_policy = target_security_level == "confirm"
    actual_confirm_delegation = confirm_delegation or needs_confirmation_due_to_policy

    if actual_confirm_delegation:
        if not exec_context.request_confirmation_callback:
            logger.error(
                f"Confirmation required for delegating to '{target_service_id}' (policy: {target_security_level}, arg: {confirm_delegation}), but no confirmation callback is available. Aborting delegation."
            )
            return f"Error: Confirmation required to delegate to '{target_service_id}', but no confirmation mechanism is available."
        else:
            # Attempt to get a description from the target service's config
            if (
                hasattr(target_service, "service_config")
                and target_service.service_config
            ):
                # Check if service_config has a description attribute or similar
                # This part is speculative as ProcessingServiceConfig doesn't directly hold 'description'
                # but the profile definition in config.yaml does.
                # For now, we'll use a generic description or the profile ID.
                # A more robust way would be to ensure profile description is accessible via ProcessingService.
                pass  # Placeholder for actual description retrieval if different

            # Use profile ID as part of the description if a more specific one isn't easily available
            profile_id_for_prompt = target_service_id
            if hasattr(target_service, "service_config") and hasattr(
                target_service.service_config, "id"
            ):  # Assuming service_config might have an id
                profile_id_for_prompt = getattr(
                    target_service.service_config, "id", target_service_id
                )

            prompt_text = (
                f"Do you want to delegate the task: '{telegramify_markdown.escape_markdown(user_request[:100])}...' "
                f"to the '{telegramify_markdown.escape_markdown(profile_id_for_prompt)}' profile?"
            )
            try:
                # Ensure the callback is correctly typed/cast if necessary
                typed_callback = cast(
                    "ConfirmationCallbackSignature",
                    exec_context.request_confirmation_callback,
                )
                # Call with positional arguments
                user_confirmed = await typed_callback(
                    exec_context.conversation_id,
                    exec_context.interface_type,
                    exec_context.turn_id,
                    prompt_text,
                    "delegate_to_service",
                    {
                        "target_service_id": target_service_id,
                        "user_request": user_request,
                        "confirm_delegation": True,
                    },
                    60.0,  # 60 second timeout
                )
                if not user_confirmed:
                    logger.info(
                        f"User cancelled delegation to service '{target_service_id}'."
                    )
                    return f"OK. Delegation to service '{target_service_id}' cancelled by user."
            except asyncio.TimeoutError:
                logger.warning(
                    f"Confirmation for delegating to '{target_service_id}' timed out."
                )
                return f"Error: Confirmation timed out for delegating to '{target_service_id}'."
            except Exception as e:
                logger.error(
                    f"Error during confirmation for delegating to '{target_service_id}': {e}",
                    exc_info=True,
                )
                return f"Error during confirmation for delegating to '{target_service_id}': {e}"

    logger.info(f"Delegating request to service profile: '{target_service_id}'")
    try:
        (
            final_text_reply,
            _final_assistant_message_id,  # Ignored
            _final_reasoning_info,  # Ignored
            error_traceback,
        ) = await target_service.handle_chat_interaction(
            db_context=exec_context.db_context,
            interface_type=exec_context.interface_type,  # Use current interface type
            conversation_id=exec_context.conversation_id,  # Use current conversation ID
            trigger_content_parts=[{"type": "text", "text": user_request}],
            trigger_interface_message_id=None,  # This is an internal trigger
            user_name=exec_context.user_name,  # Pass original user's name
            replied_to_interface_id=None,
            chat_interface=exec_context.chat_interface,  # Pass through for nested actions
            request_confirmation_callback=exec_context.request_confirmation_callback,  # Pass through
        )

        if error_traceback:
            logger.error(
                f"Delegated service '{target_service_id}' returned an error: {error_traceback}"
            )
            return f"Error from '{target_service_id}' service: An error occurred during processing."
        if final_text_reply is None:
            logger.info(
                f"Delegated service '{target_service_id}' returned no textual reply."
            )
            return f"Service '{target_service_id}' processed the request but provided no textual response."

        logger.info(
            f"Received reply from delegated service '{target_service_id}': '{final_text_reply[:100]}...'"
        )
        return final_text_reply

    except Exception as e:
        logger.error(
            f"Failed to delegate request to service '{target_service_id}': {e}",
            exc_info=True,
        )
        return f"Error: Failed to delegate task to service '{target_service_id}'. Details: {e}"
