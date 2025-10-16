"""
Router for Vite-managed React pages.

This router centralizes the handling of React pages served by Vite,
including chat, tools, and tool-test-bench interfaces.
"""

import logging
import os
import pathlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)
vite_pages_router = APIRouter()


@vite_pages_router.get("/", name="ui_root_redirect")
async def root_redirect(request: Request) -> RedirectResponse:
    """Redirects the root path to the chat interface."""
    return RedirectResponse(url="/chat", status_code=302)


def _get_dev_mode_from_request(request: Request) -> bool:
    """Get dev_mode from app config if available, otherwise from environment."""
    if hasattr(request.app.state, "config") and "dev_mode" in request.app.state.config:
        return request.app.state.config.get("dev_mode", False)
    # Fallback to environment variable
    return os.getenv("DEV_MODE", "false").lower() == "true"


def _serve_vite_html_file(request: Request, html_filename: str) -> Response:
    """Serve a Vite HTML file, either from dist (production) or frontend dir (dev)."""
    dev_mode = _get_dev_mode_from_request(request)

    if dev_mode:
        # In dev mode, serve from frontend directory
        # TODO: Replace with project root constant to reduce fragility
        frontend_dir = (
            pathlib.Path(__file__).parent.parent.parent.parent.parent / "frontend"
        )
        html_file = frontend_dir / html_filename
        if html_file.exists():
            return FileResponse(html_file, media_type="text/html")
        else:
            logger.error(
                f"Dev mode: HTML file {html_filename} not found at {html_file}. "
                f"Frontend dir exists: {frontend_dir.exists()}"
            )
            raise HTTPException(
                status_code=404,
                detail=f"HTML file {html_filename} not found in dev mode",
            )
    else:
        # In production mode, serve from dist directory
        static_dir = pathlib.Path(__file__).parent.parent.parent / "static" / "dist"
        html_file = static_dir / html_filename
        if html_file.exists():
            return FileResponse(html_file, media_type="text/html")
        else:
            # Enhanced error logging for CI debugging
            contents = []
            if static_dir.exists():
                try:
                    contents = [p.name for p in static_dir.iterdir()][
                        :10
                    ]  # Limit to first 10 files
                except OSError:
                    contents = ["Error reading directory"]

            logger.error(
                f"Production mode: HTML file {html_filename} not found at {html_file}. "
                f"Static dir exists: {static_dir.exists()}. "
                f"Contents (first 10): {contents}"
            )
            raise HTTPException(
                status_code=404,
                detail=f"Built HTML file {html_filename} not found in production mode",
            )


@vite_pages_router.get("/chat", name="chat_ui")
async def chat_ui(request: Request) -> Response:
    """Serve the React chat interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/context", name="context_ui")
async def context_ui(request: Request) -> Response:
    """Serve the React context page via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/tools", name="tools_ui")
async def tools_ui(request: Request) -> Response:
    """Serve the React tools interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/tool-test-bench", name="tool_test_bench_ui")
async def tool_test_bench_ui(request: Request) -> Response:
    """Serve the React tool test bench interface."""
    return _serve_vite_html_file(request, "tool-test-bench.html")


@vite_pages_router.get("/errors", name="errors_ui")
@vite_pages_router.get("/errors/{error_id:int}", name="error_detail_ui")
async def errors_ui(request: Request) -> Response:
    """Serve the React errors interface."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/notes", name="notes_ui")
@vite_pages_router.get("/notes/add", name="notes_add_ui")
@vite_pages_router.get("/notes/edit/{title:str}", name="notes_edit_ui")
async def notes_ui(request: Request) -> Response:
    """Serve the React notes interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/tasks", name="tasks_ui")
async def tasks_ui(request: Request) -> Response:
    """Serve the React tasks interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/event-listeners", name="event_listeners_ui")
@vite_pages_router.get("/event-listeners/new", name="event_listeners_new_ui")
@vite_pages_router.get(
    "/event-listeners/{listener_id:int}", name="event_listener_detail_ui"
)
@vite_pages_router.get(
    "/event-listeners/{listener_id:int}/edit", name="event_listener_edit_ui"
)
async def event_listeners_ui(request: Request) -> Response:
    """Serve the React event listeners interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/automations", name="automations_ui")
@vite_pages_router.get("/automations/create/event", name="automations_create_event_ui")
@vite_pages_router.get(
    "/automations/create/schedule", name="automations_create_schedule_ui"
)
@vite_pages_router.get(
    "/automations/{type}/{automation_id:int}", name="automation_detail_ui"
)
async def automations_ui(request: Request) -> Response:
    """Serve the React automations interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/history", name="history_ui")
@vite_pages_router.get("/history/{conversation_id:str}", name="history_detail_ui")
async def history_ui(request: Request) -> Response:
    """Serve the React history interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/events", name="events_ui")
@vite_pages_router.get("/events/{event_id:str}", name="events_detail_ui")
async def events_ui(request: Request) -> Response:
    """Serve the React events interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/docs", name="docs_ui")
@vite_pages_router.get("/docs/{filename:path}", name="docs_detail_ui")
async def docs_ui(request: Request) -> Response:
    """Serve the React documentation interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/settings/tokens", name="settings_tokens_ui")
async def settings_tokens_ui(request: Request) -> Response:
    """Serve the React settings/tokens interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/documents", name="documents_ui")
@vite_pages_router.get("/documents/", name="documents_ui_trailing")
@vite_pages_router.get("/documents/upload", name="documents_upload_ui")
@vite_pages_router.get("/documents/{document_id:str}", name="documents_detail_ui")
async def documents_ui(request: Request) -> Response:
    """Serve the React documents interface via router."""
    return _serve_vite_html_file(request, "router.html")


@vite_pages_router.get("/vector-search", name="vector_search_ui")
async def vector_search_ui(request: Request) -> Response:
    """Serve the React vector search interface via router."""
    return _serve_vite_html_file(request, "router.html")
