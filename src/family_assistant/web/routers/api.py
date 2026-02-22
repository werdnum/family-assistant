import logging

from fastapi import APIRouter

from .a2a_api import a2a_router
from .attachments_api import attachments_api_router
from .automations_api import automations_api_router
from .chat_api import chat_api_router
from .context_viewer import context_viewer_router
from .debug_api import debug_api_router
from .diagnostics_api import diagnostics_api_router
from .documents_api import documents_api_router
from .errors_api import errors_api_router
from .events_api import events_api_router
from .notes_api import notes_api_router
from .tasks_api import tasks_api_router
from .tools_api import tools_api_router
from .vector_search_api import vector_search_api_router
from .version_api import version_router

logger = logging.getLogger(__name__)
api_router = APIRouter()

# Include the individual routers
api_router.include_router(tools_api_router, prefix="/tools", tags=["Tools Execution"])
api_router.include_router(
    documents_api_router, prefix="/documents", tags=["Document Ingestion"]
)
api_router.include_router(errors_api_router, prefix="/errors", tags=["Error Logs"])
api_router.include_router(
    automations_api_router, prefix="/automations", tags=["Automations"]
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
api_router.include_router(
    attachments_api_router, prefix="/attachments", tags=["Attachments"]
)
api_router.include_router(debug_api_router, prefix="/debug", tags=["Debug"])
api_router.include_router(
    diagnostics_api_router, prefix="/diagnostics", tags=["Diagnostics"]
)
api_router.include_router(version_router, tags=["Version"])
api_router.include_router(a2a_router, prefix="/a2a", tags=["A2A Protocol"])
