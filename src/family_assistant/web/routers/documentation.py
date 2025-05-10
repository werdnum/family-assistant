import logging

import aiofiles
from fastapi import APIRouter, HTTPException, Request, status  # Added status
from fastapi.responses import HTMLResponse, RedirectResponse

from family_assistant.tools import (
    _scan_user_docs,  # Assuming this is the correct location
)

from ..auth import AUTH_ENABLED  # Use relative import
from ..utils import md_renderer  # Use relative import

logger = logging.getLogger(__name__)
documentation_router = APIRouter()

# docs_user_dir and SERVER_URL will be set on app.state by app_creator


@documentation_router.get("/docs/")
async def redirect_to_user_guide() -> RedirectResponse:
    """Redirects the base /docs/ path to the main user guide."""
    return RedirectResponse(
        url="/docs/USER_GUIDE.md", status_code=status.HTTP_302_FOUND
    )


@documentation_router.get("/docs/{filename:path}", response_class=HTMLResponse)
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
        available_docs = _scan_user_docs()

        return templates.TemplateResponse(
            "doc_page.html",
            {
                "request": request,
                "content": content_html,
                "title": filename,
                "available_docs": available_docs,
                "server_url": server_url,
                "user": request.session.get("user"),
                "auth_enabled": AUTH_ENABLED,
            },
        )
    except Exception as e:
        logger.error(
            f"Error serving documentation file '{filename}': {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Error rendering documentation."
        ) from e
