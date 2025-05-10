import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from family_assistant.storage import get_all_tasks
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import AUTH_ENABLED
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)
tasks_ui_router = APIRouter()


@tasks_ui_router.get("/tasks", response_class=HTMLResponse)
async def view_tasks(
    request: Request, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> HTMLResponse:
    """Serves the page displaying scheduled tasks."""
    templates = request.app.state.templates
    try:
        tasks = await get_all_tasks(db_context, limit=200)  # Pass context, fetch tasks
        return templates.TemplateResponse(
            "tasks.html",
            {
                "request": request,
                "tasks": tasks,
                "user": request.session.get("user"),
                "auth_enabled": AUTH_ENABLED,
            },
        )
    except Exception as e:
        logger.error(f"Error fetching tasks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch tasks") from e
