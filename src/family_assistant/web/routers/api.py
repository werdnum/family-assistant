import logging

from fastapi import APIRouter

from .chat_api import chat_api_router
from .documents_api import documents_api_router
from .errors_api import errors_api_router
from .tools_api import tools_api_router

logger = logging.getLogger(__name__)
api_router = APIRouter()

# Include the individual routers
api_router.include_router(tools_api_router, prefix="/tools", tags=["Tools Execution"])
api_router.include_router(
    documents_api_router, prefix="/documents", tags=["Document Ingestion"]
)
api_router.include_router(errors_api_router, prefix="/errors", tags=["Error Logs"])
api_router.include_router(
    chat_api_router, tags=["Chat API"]
)  # No prefix for /v1/chat/send_message
