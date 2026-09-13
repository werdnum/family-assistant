from fastapi import APIRouter
from pydantic import BaseModel

from family_assistant import __version__
from family_assistant.build_info import get_build_date, get_git_commit

version_router = APIRouter()


class VersionInfo(BaseModel):
    version: str
    git_commit: str
    build_date: str


@version_router.get("/version")
async def get_version() -> VersionInfo:
    """Returns the current version, git commit, and build date."""
    return VersionInfo(
        version=__version__,
        git_commit=get_git_commit(),
        build_date=get_build_date(),
    )
