"""Deliver a finished delegation's result as a task of its own.

Delivering a result can mean waking the profile that delegated, which runs a
whole turn. That turn gets its own task and handler budget rather than whatever
is left of the task that noticed the run had finished: a poll, a reconcile, or
the hourly cleanup. Every one of them queues delivery through here.

There is one delivery task per delegation, under a deterministic id. A
delivery already pending or running owns the work, so queuing again leaves it
alone; a finished one is replaced, because a run that is queued for delivery
again was not delivered by it.

The delivery task does not host the wake turn: it launches the turn on the
background turn runner and returns (see
docs/design/background-turns-off-the-task-worker.md). The turn holds a turn
lease (see ``turn_resumption``). When its process goes away mid-turn the lease
comes due, and :class:`DelegationWakeResumer` queues the delivery again, which
resumes the turn from the rows it already persisted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NotRequired, TypedDict

from family_assistant.storage.tasks import TaskAlreadyExistsError, TaskPriority

if TYPE_CHECKING:
    from family_assistant.services.turn_resumption import (
        TurnLeaseRegistry,
        TurnResumePayload,
    )
    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.tasks import TasksRepository

logger = logging.getLogger(__name__)

DELEGATION_DELIVERY_TASK_TYPE = "delegation_delivery"
DELEGATION_WAKE_RESUMER = "delegation_wake"


class DelegationDeliveryPayload(TypedDict):
    """Payload for delegation_delivery tasks."""

    delegation_id: str
    # Deliver even if the caller never handed the run off (see
    # ``TaskWorker._notify_delegation_if_needed``).
    force: bool
    # Set when the delivery resumes a wake turn its lease handed on: how many
    # times that turn has been interrupted, so resumption stays bounded.
    wake_attempt: NotRequired[int]


def delegation_delivery_task_id(delegation_id: str) -> str:
    return f"{DELEGATION_DELIVERY_TASK_TYPE}:{delegation_id}"


async def queue_delegation_delivery(
    tasks: TasksRepository,
    delegation_id: str,
    *,
    force: bool,
    wake_attempt: int | None = None,
) -> None:
    """Make sure a delivery of ``delegation_id`` is pending or running."""
    task_id = delegation_delivery_task_id(delegation_id)
    payload: DelegationDeliveryPayload = {
        "delegation_id": delegation_id,
        "force": force,
    }
    if wake_attempt is not None:
        payload["wake_attempt"] = wake_attempt
    await tasks.delete_finished(task_id)
    if await _enqueue_delivery(tasks, task_id, payload):
        return
    # The row in the way may have finished between the delete and the insert,
    # in which case nothing is left to deliver. Once more settles it: a row
    # that is still there now is active.
    if await tasks.delete_finished(task_id) and await _enqueue_delivery(
        tasks, task_id, payload
    ):
        return
    logger.info(
        "Delivery of delegation %s is already queued or running.", delegation_id
    )


async def _enqueue_delivery(
    tasks: TasksRepository, task_id: str, payload: DelegationDeliveryPayload
) -> bool:
    try:
        await tasks.enqueue(
            task_id=task_id,
            task_type=DELEGATION_DELIVERY_TASK_TYPE,
            payload=payload,
            priority=TaskPriority.INTERACTIVE,
        )
    except TaskAlreadyExistsError:
        return False
    return True


class DelegationWakeResumer:
    """Resumes interrupted delegation wake turns through a fresh delivery."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def resume(
        self, payload: TurnResumePayload, registry: TurnLeaseRegistry
    ) -> bool:
        del registry  # The delivery arms the resumed turn's lease itself.
        # A wake turn exists only because its delivery was due, so the delivery
        # that resumes it is forced.
        await queue_delegation_delivery(
            self._database.tasks,
            self._delegation_id(payload),
            force=True,
            wake_attempt=payload.attempt + 1,
        )
        return True

    async def deliver_pending_reply(self, payload: TurnResumePayload) -> None:
        # The turn has its final reply and only sending it is left.
        await queue_delegation_delivery(
            self._database.tasks, self._delegation_id(payload), force=True
        )

    @staticmethod
    def _delegation_id(payload: TurnResumePayload) -> str:
        if payload.delegation_id is None:
            raise ValueError(
                f"Delegation wake turn {payload.turn_id} has no delegation id"
            )
        return payload.delegation_id
