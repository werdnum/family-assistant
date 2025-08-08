"""API endpoints for documentation access."""

import logging
from pathlib import Path

import aiofiles
from fastapi import APIRouter, HTTPException, Request

from family_assistant.tools import _scan_user_docs

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", summary="List available documentation files")
async def list_documentation(request: Request) -> list[str]:
    """List all available documentation files."""
    docs_user_dir = request.app.state.docs_user_dir
    try:
        available_docs = _scan_user_docs(docs_user_dir)
        return available_docs
    except Exception as e:
        logger.error(f"Error listing documentation files: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Error listing documentation files"
        ) from e


@router.get("/{filename:path}", summary="Get documentation content")
async def get_documentation(request: Request, filename: str) -> dict:
    """Get the content of a specific documentation file."""
    docs_user_dir = request.app.state.docs_user_dir
    server_url = request.app.state.server_url

    # Security checks
    allowed_extensions = {".md"}
    doc_path = (docs_user_dir / filename).resolve()

    if docs_user_dir not in doc_path.parents and doc_path != docs_user_dir:
        logger.warning(f"Attempted directory traversal access to: {doc_path}")
        raise HTTPException(status_code=404, detail="Document not found")

    if doc_path.suffix not in allowed_extensions:
        logger.warning(f"Attempted access to non-markdown file: {doc_path}")
        raise HTTPException(status_code=404, detail="Document not found")

    if not doc_path.is_file():
        logger.warning(f"Documentation file not found: {doc_path}")
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        async with aiofiles.open(doc_path, encoding="utf-8") as f:
            content_md = await f.read()

        # Replace SERVER_URL placeholder
        content_md_processed = content_md.replace("{{ SERVER_URL }}", server_url)

        return {
            "filename": filename,
            "content": content_md_processed,
            "title": Path(filename).stem.replace("_", " ").title(),
        }
    except Exception as e:
        logger.error(
            f"Error reading documentation file '{filename}': {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Error reading documentation file"
        ) from e
