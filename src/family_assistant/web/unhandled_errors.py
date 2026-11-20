"""Route unhandled request exceptions into the application's error log.

uvicorn reports an exception that escapes the app on its own ``uvicorn.error``
logger, and its default logging config stops that logger propagating to the
root logger. The database error handler is attached to the root logger, so
without this a 500 reaches stdout but never the ``error_logs`` table that the
engineer profile and the diagnostics export read.

Starlette's ``ServerErrorMiddleware`` calls the handler registered for
``Exception`` before re-raising to the server, so logging here adds a record
on an application logger without changing the response or uvicorn's own line.
WebSocket connections do not pass through that middleware and are not covered.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

logger = logging.getLogger(__name__)


async def _log_unhandled_exception(request: Request, exc: Exception) -> Response:
    # The query string is left out: it can carry tokens.
    logger.error(
        "Unhandled exception in %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return PlainTextResponse("Internal Server Error", status_code=500)


def install_unhandled_exception_logging(app: FastAPI) -> None:
    """Log every request exception that would otherwise reach only uvicorn."""
    app.add_exception_handler(Exception, _log_unhandled_exception)
