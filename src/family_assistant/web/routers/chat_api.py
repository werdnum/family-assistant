import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from family_assistant import storage
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db, get_processing_service
from family_assistant.web.models import ChatMessageResponse, ChatPromptRequest

logger = logging.getLogger(__name__)
chat_api_router = APIRouter()


@chat_api_router.post("/v1/chat/send_message")  # Path relative to the prefix in api.py
async def api_chat_send_message(
    payload: ChatPromptRequest,
    request: Request,  # To access app.state for config if needed by ProcessingService context
    processing_service: Annotated[ProcessingService, Depends(get_processing_service)],
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> ChatMessageResponse:
    """
    Receives a user prompt via API, processes it using the ProcessingService,
    and returns the assistant's reply.
    """
    conversation_id = payload.conversation_id or f"api_conv_{uuid.uuid4()}"
    turn_id = f"api_turn_{uuid.uuid4()}"

    logger.info(
        f"API chat request received. Conversation ID: {conversation_id}, Turn ID: {turn_id}, Prompt: '{payload.prompt[:100]}...'"
    )

    # Prepare messages for ProcessingService
    user_message = {"role": "user", "content": payload.prompt}
    # For API, each call is typically independent unless conversation_id is managed by client
    # If conversation_id is provided, history could be fetched, but for now, simple single message processing.
    messages_to_process = [user_message]  # This is for the LLM, not for saving directly

    timestamp_now = datetime.now(timezone.utc)
    thread_root_id_for_turn: int | None = None

    # 1. Save the initial user prompt
    user_message_to_save = {
        "interface_type": "api",
        "conversation_id": conversation_id,
        "interface_message_id": None,  # No specific interface ID for the API prompt itself
        "turn_id": turn_id,
        "thread_root_id": None,  # Will be updated after saving if it's the first message
        "timestamp": timestamp_now,
        "role": "user",
        "content": payload.prompt,
        "tool_calls": None,
        "tool_call_id": None,
        "reasoning_info": None,
        "error_traceback": None,
    }
    saved_user_msg_record = await storage.add_message_to_history(
        db_context=db_context, **user_message_to_save
    )
    if saved_user_msg_record and saved_user_msg_record.get("internal_id") is not None:
        thread_root_id_for_turn = saved_user_msg_record["internal_id"]
        # If this was the first message, its own ID is the root.
        # No need to update the already saved record's thread_root_id here,
        # as add_message_to_history handles setting it if it was None.

    # Call process_message
    # The API interface doesn't directly send messages back via a ChatInterface like Telegram.
    # The final LLM response content is what we need for the API response.
    # `process_message` returns messages specific to the assistant's processing (tool calls, final reply etc.)
    processed_turn_messages, _ = await processing_service.process_message(
        db_context=db_context,
        messages=messages_to_process,  # Pass the simple list for LLM
        interface_type="api",
        conversation_id=conversation_id,
        turn_id=turn_id,
        chat_interface=None,  # API handles response directly, no callback interface
        new_task_event=None,  # No task event for synchronous API response
    )

    # 2. Save the generated turn messages
    final_reply_content: str | None = None
    for _, msg_dict in enumerate(processed_turn_messages):
        message_to_save = {
            "interface_type": "api",
            "conversation_id": conversation_id,
            "interface_message_id": None,  # Agent messages don't have this from API
            "turn_id": turn_id,
            "thread_root_id": thread_root_id_for_turn,
            "timestamp": datetime.now(
                timezone.utc
            ),  # More precise timestamp per message
            "role": msg_dict.get("role"),
            "content": msg_dict.get("content"),
            "tool_calls": msg_dict.get("tool_calls"),
            "tool_call_id": msg_dict.get("tool_call_id"),
            "reasoning_info": msg_dict.get("reasoning_info"),
            "error_traceback": msg_dict.get("error_traceback"),
        }
        await storage.add_message_to_history(db_context=db_context, **message_to_save)
        if msg_dict.get("role") == "assistant" and msg_dict.get("content"):
            # The last assistant message with content is considered the final reply
            final_reply_content = msg_dict["content"]

    if final_reply_content is None:
        # This case might occur if the LLM only makes tool calls without a textual reply,
        # or if an error occurred that process_message handled by returning no content.
        # Also, if processed_turn_messages was empty.
        logger.error(
            f"No final assistant reply content found for API chat. Conversation ID: {conversation_id}, Turn ID: {turn_id}"
        )
        # Depending on desired behavior, could return an error or an empty reply.
        # The test_api_chat_add_note_tool expects a final textual reply.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Assistant did not provide a textual reply.",
        )

    return ChatMessageResponse(
        reply=final_reply_content,
        conversation_id=conversation_id,
        turn_id=turn_id,
    )
