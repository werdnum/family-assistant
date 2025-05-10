import logging

import telegram.error
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
health_router = APIRouter()


@health_router.get("/health", status_code=status.HTTP_200_OK)
async def health_check(request: Request) -> JSONResponse:
    """Checks basic service health and Telegram polling status."""
    telegram_service = getattr(request.app.state, "telegram_service", None)

    if (
        not telegram_service
        or not hasattr(telegram_service, "application")
        or not hasattr(telegram_service.application, "updater")
    ):
        # Service not initialized or structure unexpected
        return JSONResponse(
            content={
                "status": "unhealthy",
                "reason": "Telegram service not initialized",
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Check if polling was ever started and if it's currently running
    was_started = getattr(telegram_service, "_was_started", False)
    is_running = telegram_service.application.updater.running

    if was_started and not is_running:
        # Polling was started but has stopped
        last_error = getattr(telegram_service, "last_error", None)
        reason = "Telegram polling stopped"
        if isinstance(last_error, telegram.error.Conflict):
            reason = f"Telegram polling stopped due to Conflict error: {last_error}"
            logger.warning(
                f"Health check failing due to Telegram Conflict: {last_error}"
            )  # Log warning
        elif last_error:
            reason = (
                f"Telegram polling stopped. Last error: {type(last_error).__name__}"
            )
            logger.warning(
                f"Health check failing because Telegram polling stopped. Last error: {last_error}"
            )  # Log warning
        else:
            logger.warning(
                "Health check failing because Telegram polling stopped (no specific error recorded)."
            )  # Log warning

        return JSONResponse(
            content={"status": "unhealthy", "reason": reason},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    elif not was_started:
        # Polling hasn't been started yet (still initializing)
        return JSONResponse(
            content={
                "status": "initializing",
                "reason": "Telegram service initializing",
            },
            status_code=status.HTTP_200_OK,  # Or 503 if you prefer to fail until fully ready
        )
    else:
        # Polling was started and is running
        return JSONResponse(
            content={"status": "ok", "reason": "Telegram polling active"},
            status_code=status.HTTP_200_OK,
        )
