"""React pages for native clients, protected by the app JWT auth boundary."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from family_assistant.paths import STATIC_DIST_DIR

embedded_pages_router = APIRouter()


@embedded_pages_router.get("")
@embedded_pages_router.get("/{page_path:path}")
async def embedded_page() -> FileResponse:
    """Serve the embedded React router; all nested paths share its HTML entry."""
    html_file = STATIC_DIST_DIR / "embedded" / "embedded.html"
    if not html_file.is_file():
        raise HTTPException(status_code=503, detail="Embedded frontend is not built.")
    return FileResponse(html_file, headers={"Cache-Control": "no-store"})
