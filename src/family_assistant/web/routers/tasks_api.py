from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

tasks_api_router = APIRouter()


class TaskModel(BaseModel):
    id: int
    task_id: str
    task_type: str
    payload: dict | None = None
    status: str
    created_at: datetime
    scheduled_at: datetime | None = None
    retry_count: int
    max_retries: int
    recurrence_rule: str | None = None
    error_message: str | None = None


class TaskListResponse(BaseModel):
    tasks: list[TaskModel]


@tasks_api_router.get("/")
async def list_tasks(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    task_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: str = "desc",
    limit: int = 100,
) -> TaskListResponse:
    """Return tasks with optional filtering."""
    tasks = await db_context.tasks.get_all(
        status=status_filter,
        task_type=task_type,
        date_from=date_from,
        date_to=date_to,
        sort_order=sort,
        limit=limit,
    )
    return TaskListResponse(tasks=[TaskModel(**task) for task in tasks])


@tasks_api_router.post(
    "/{internal_task_id}/retry", status_code=status.HTTP_202_ACCEPTED
)
async def retry_task(
    internal_task_id: int,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> dict[str, str]:
    """Manually retry a task."""
    success = await db_context.tasks.manually_retry(internal_task_id)
    if not success:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Task not found or not retryable"
        )
    return {"message": "Retry scheduled"}
