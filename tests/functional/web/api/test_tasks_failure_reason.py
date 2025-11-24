from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import insert

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import tasks_table


@pytest.mark.asyncio
async def test_tasks_list_returns_error_message(
    api_test_client: AsyncClient,
    api_db_context: DatabaseContext,
) -> None:
    """Test that the tasks list endpoint returns the error message for failed tasks."""

    # 1. Insert a failed task directly into the database
    task_id = "failed_task_1"
    error_message = "This task failed due to some error"

    stmt = insert(tasks_table).values(
        task_id=task_id,
        task_type="test_task",
        status="failed",
        error=error_message,
        created_at=datetime.now(UTC),
        retry_count=3,
        max_retries=3,
    )
    await api_db_context.execute_with_retry(stmt)

    # 2. Call the tasks API list endpoint
    response = await api_test_client.get("/api/tasks/")
    assert response.status_code == 200

    data = response.json()
    assert "tasks" in data

    # 3. Find our task
    found_task = None
    for task in data["tasks"]:
        if task["task_id"] == task_id:
            found_task = task
            break

    assert found_task is not None

    # 4. Verify that the error message is present
    # This assertion verifies that the API correctly exposes the failure reason
    assert found_task["error_message"] == error_message
