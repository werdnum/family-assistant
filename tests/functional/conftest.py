"""Disable auth for all functional tests to prevent OIDC redirects."""

import os
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import get_db

# IMPORTANT: Disable auth BEFORE importing any web modules
# This must happen at the top level of the functional test package
# to ensure it takes effect before any web modules are imported
os.environ["OIDC_CLIENT_ID"] = ""
os.environ["OIDC_CLIENT_SECRET"] = ""
os.environ["OIDC_DISCOVERY_URL"] = ""
os.environ["SESSION_SECRET_KEY"] = ""

# This file ensures auth is disabled for ALL functional tests,
# not just the web tests. This prevents issues when tests run
# in parallel and import web modules in different orders.


@pytest.fixture
async def api_client(
    db_engine: AsyncEngine,
) -> AsyncGenerator[httpx.AsyncClient]:
    """Provide an HTTP client for testing FastAPI endpoints."""

    async def override_get_db() -> AsyncGenerator[DatabaseContext]:
        async with DatabaseContext(engine=db_engine) as db:
            yield db

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.state.embedding_generator = MockEmbeddingGenerator(
        model_name="test", dimensions=3
    )
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client
    fastapi_app.dependency_overrides.clear()
