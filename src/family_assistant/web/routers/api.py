import logging

from fastapi import APIRouter

from .chat_api import chat_api_router
from .context_viewer import context_viewer_router
from .documents_api import documents_api_router
from .errors_api import errors_api_router
from .events_api import events_api_router
from .listeners_api import listeners_api_router
from .notes_api import notes_api_router
from .tasks_api import tasks_api_router
from .tools_api import tools_api_router
from .vector_search_api import vector_search_api_router

logger = logging.getLogger(__name__)
api_router = APIRouter()

# Include the individual routers
api_router.include_router(tools_api_router, prefix="/tools", tags=["Tools Execution"])
api_router.include_router(
    documents_api_router, prefix="/documents", tags=["Document Ingestion"]
)
api_router.include_router(errors_api_router, prefix="/errors", tags=["Error Logs"])
api_router.include_router(
    listeners_api_router, prefix="/event-listeners", tags=["Event Listeners"]
)
api_router.include_router(
    chat_api_router, tags=["Chat API"]
)  # No prefix for /v1/chat/send_message
api_router.include_router(notes_api_router, prefix="/notes", tags=["Notes"])
api_router.include_router(tasks_api_router, prefix="/tasks", tags=["Tasks"])
api_router.include_router(events_api_router, prefix="/events", tags=["Events"])
api_router.include_router(
    vector_search_api_router, prefix="/vector-search", tags=["Vector Search"]
)
api_router.include_router(context_viewer_router, tags=["Context API"])
