"""Unhandled request exceptions reach an application logger, not only uvicorn's."""

import logging

import httpx
import pytest
from fastapi import FastAPI

from family_assistant.web.unhandled_errors import install_unhandled_exception_logging


def _failing_app() -> FastAPI:
    app = FastAPI()
    install_unhandled_exception_logging(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("serialization went wrong")

    return app


@pytest.mark.asyncio
async def test_unhandled_exception_is_logged_where_the_error_log_handler_sees_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The database error handler sits on the root logger at ERROR."""
    transport = httpx.ASGITransport(app=_failing_app(), raise_app_exceptions=False)

    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get("/boom?token=secret")

    assert response.status_code == 500
    records = [r for r in caplog.records if r.name.startswith("family_assistant.")]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)
    assert "GET /boom" in record.getMessage()
    assert "secret" not in record.getMessage()
