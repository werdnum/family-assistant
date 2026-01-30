"""Storage repository implementations."""

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

__all__ = [
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
]
