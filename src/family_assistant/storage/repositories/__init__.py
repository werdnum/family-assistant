"""Storage repository implementations."""

from .a2a_tasks import A2ATasksRepository
from .automations import AutomationsRepository
from .base import BaseRepository
from .email import EmailRepository
from .error_logs import ErrorLogsRepository
from .events import EventsRepository
from .message_history import MessageHistoryRepository
from .notes import NotesRepository
from .push_subscription import PushSubscriptionRepository
from .schedule_automations import ScheduleAutomationsRepository
from .tasks import TasksRepository
from .vector import VectorRepository
from .worker_tasks import WorkerTasksRepository

__all__ = [
    "A2ATasksRepository",
    "AutomationsRepository",
    "BaseRepository",
    "EmailRepository",
    "ErrorLogsRepository",
    "EventsRepository",
    "MessageHistoryRepository",
    "NotesRepository",
    "PushSubscriptionRepository",
    "ScheduleAutomationsRepository",
    "TasksRepository",
    "VectorRepository",
    "WorkerTasksRepository",
]
