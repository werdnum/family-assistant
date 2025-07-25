"""Web UI router for the chat interface conversations list."""

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import get_current_user_optional
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


# In development, Vite serves /chat directly
# In production, the HTML file is served by the catch-all handler


@router.get("/chat/conversations", response_class=HTMLResponse)
async def chat_conversations(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    current_user: Annotated[dict | None, Depends(get_current_user_optional)],
) -> HTMLResponse:
    """Display a list of conversations for the chat interface."""
    templates = request.app.state.templates

    # Get all conversations grouped by interface and conversation ID
    history_by_chat = await db_context.message_history.get_all_grouped(
        interface_type="web"
    )

    # Extract unique conversation IDs with their latest message
    conversations = []
    for (_interface_type, conversation_id), messages in history_by_chat.items():
        if messages:
            latest_message = messages[-1]
            conversations.append({
                "conversation_id": conversation_id,
                "last_message": latest_message.get("content", ""),
                "last_timestamp": latest_message.get("timestamp"),
                "message_count": len(messages),
            })

    # Sort by last timestamp, most recent first
    conversations.sort(key=lambda x: x["last_timestamp"], reverse=True)
    conversations = conversations[:20]  # Limit to 20 most recent

    return templates.TemplateResponse(
        request=request,
        name="chat_conversations.html.j2",
        context={
            "request": request,
            "current_user": current_user,
            "conversations": conversations,
            "now_utc": datetime.now(timezone.utc),
        },
    )
