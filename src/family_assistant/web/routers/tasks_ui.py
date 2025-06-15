import logging
from datetime import datetime, timezone  # Added
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse  # Added RedirectResponse

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import AUTH_ENABLED
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)
tasks_ui_router = APIRouter()


@tasks_ui_router.get("/tasks", response_class=HTMLResponse, name="ui_list_tasks")
async def view_tasks(
    request: Request, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> HTMLResponse:
    """Serves the page displaying scheduled tasks with filtering and sorting."""
    templates = request.app.state.templates

    # Extract filter parameters from query string
    status = request.query_params.get("status")
    task_type = request.query_params.get("task_type")
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    sort = request.query_params.get("sort", "asc")  # Default to oldest first

    # Parse date filters if provided
    date_from_dt = None
    date_to_dt = None
    try:
        if date_from:
            date_from_dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
        if date_to:
            date_to_dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(
            f"Invalid date format in filters: from={date_from}, to={date_to}"
        )

    try:
        # Get filtered tasks
        tasks = await db_context.tasks.get_all(
            status=status if status else None,
            task_type=task_type if task_type else None,
            date_from=date_from_dt,
            date_to=date_to_dt,
            sort_order=sort,
            limit=500,
        )

        # Get unique task types for autocomplete
        all_tasks_for_types = await db_context.tasks.get_all(limit=1000)
        task_types = sorted(set(task["task_type"] for task in all_tasks_for_types))

        # Check if any filters are active
        active_filters = any([status, task_type, date_from, date_to, sort != "asc"])

        return templates.TemplateResponse(
            "tasks.html.j2",
            {
                "request": request,
                "tasks": tasks,
                "task_types": task_types,
                "active_filters": active_filters,
                "user": request.session.get("user"),
                "AUTH_ENABLED": AUTH_ENABLED,  # Pass to base template
                "now_utc": datetime.now(timezone.utc),  # Pass to base template
            },
        )
    except Exception as e:
        logger.error(f"Error fetching tasks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch tasks") from e


@tasks_ui_router.post(
    "/tasks/{internal_task_id}/retry", name="ui_retry_task"
)  # New route
async def retry_task_manually_endpoint(
    request: Request,
    internal_task_id: int,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> RedirectResponse:
    """Handles the request to manually retry a task."""
    try:
        success = await db_context.tasks.manually_retry(internal_task_id)
        if success:
            logger.info(
                f"Successfully queued manual retry for task with internal ID {internal_task_id}"
            )
            # TODO: Add flash message for success if a system is in place
        else:
            logger.warning(
                f"Failed to queue manual retry for task with internal ID {internal_task_id}. It might not exist or not be in a retryable state."
            )
            # TODO: Add flash message for failure if a system is in place
    except Exception as e:
        logger.error(
            f"Error during manual retry attempt for task internal ID {internal_task_id}: {e}",
            exc_info=True,
        )
        # TODO: Add flash message for error if a system is in place
        # Fall through to redirect even on error
    return RedirectResponse(
        url=request.url_for("ui_list_tasks"), status_code=303
    )  # PRG pattern
