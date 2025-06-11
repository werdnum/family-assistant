import logging
from datetime import datetime, timezone  # Added

import aiofiles
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from family_assistant.tools import (
    _scan_user_docs,  # Assuming this is the correct location
)
from family_assistant.web.auth import AUTH_ENABLED  # Use absolute import
from family_assistant.web.utils import md_renderer  # Use absolute import

logger = logging.getLogger(__name__)
documentation_router = APIRouter()

# docs_user_dir and SERVER_URL will be set on app.state by app_creator


@documentation_router.get("/docs/", name="ui_list_docs")  # Name for base.html link
async def redirect_to_user_guide(
    request: Request,
) -> RedirectResponse:  # Add request for url_for
    """Redirects the base /docs/ path to the main user guide."""
    # Use url_path_for to construct the redirect URL robustly
    return RedirectResponse(
        url=request.app.url_path_for("ui_view_doc", filename="USER_GUIDE.md"),
        status_code=status.HTTP_302_FOUND,
    )


@documentation_router.get(
    "/docs/{filename:path}", response_class=HTMLResponse, name="ui_view_doc"
)
async def serve_documentation(request: Request, filename: str) -> HTMLResponse:
    """Serves rendered Markdown documentation files from the docs/user directory."""
    templates = request.app.state.templates
    docs_user_dir = request.app.state.docs_user_dir
    server_url = request.app.state.server_url

    allowed_extensions = {".md"}
    doc_path = (docs_user_dir / filename).resolve()

    if docs_user_dir not in doc_path.parents:
        logger.warning(f"Attempted directory traversal access to: {doc_path}")
        raise HTTPException(
            status_code=404, detail="Document not found (invalid path)."
        )
    if doc_path.suffix not in allowed_extensions:
        logger.warning(f"Attempted access to non-markdown file: {doc_path}")
        raise HTTPException(
            status_code=404, detail="Document not found (invalid file type)."
        )
    if not doc_path.is_file():
        logger.warning(f"Documentation file not found: {doc_path}")
        raise HTTPException(status_code=404, detail="Document not found.")

    try:
        async with aiofiles.open(doc_path, encoding="utf-8") as f:
            content_md = await f.read()

        content_md_processed = content_md.replace("{{ SERVER_URL }}", server_url)
        content_html = md_renderer.render(content_md_processed)
        available_docs = _scan_user_docs(docs_user_dir)

        return templates.TemplateResponse(
            "doc_page.html.j2",
            {
                "request": request,
                "content": content_html,
                "title": filename,
                "available_docs": available_docs,
                "server_url": server_url,  # Keep for {{ SERVER_URL }} replacement in md
                "user": request.session.get("user"),
                "AUTH_ENABLED": AUTH_ENABLED,  # Pass to base template
                "now_utc": datetime.now(timezone.utc),  # Pass to base template
            },
        )
    except Exception as e:
        logger.error(
            f"Error serving documentation file '{filename}': {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Error rendering documentation."
        ) from e
