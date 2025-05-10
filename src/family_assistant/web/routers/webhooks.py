import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from family_assistant.storage import store_incoming_email
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)
webhooks_router = APIRouter()

# Directory to save raw webhook request bodies for debugging/replay
# This might need to be passed via app.state if it's configurable at runtime
# For now, assuming it's a fixed path or an environment variable read here.
MAILBOX_RAW_DIR = os.getenv(
    "MAILBOX_RAW_DIR", "/mnt/data/mailbox/raw_requests"
)  # Default if not set


@webhooks_router.post("/webhook/mail")
async def handle_mail_webhook(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> Response:
    """
    Receives incoming email via webhook (expects multipart/form-data).
    Logs the received form data for now.
    """
    logger.info("Received POST request on /webhook/mail")
    try:
        # --- Save raw request body for debugging/replay ---
        raw_body = await request.body()
        try:
            # Ensure MAILBOX_RAW_DIR is accessible from the router
            # If it's set on app.state, retrieve it:
            # mailbox_raw_dir = request.app.state.mailbox_raw_dir
            # For now, using the module-level constant.
            os.makedirs(MAILBOX_RAW_DIR, exist_ok=True)
            now = datetime.now(timezone.utc)
            timestamp_str = now.strftime("%Y%m%d_%H%M%S_%f")
            content_type = request.headers.get("content-type", "unknown_content_type")
            safe_content_type = (
                re.sub(r'[<>:"/\\|?*]', "_", content_type).split(";")[0].strip()
            )
            filename = f"{timestamp_str}_{safe_content_type}.raw"
            filepath = os.path.join(MAILBOX_RAW_DIR, filename)

            async with aiofiles.open(filepath, "wb") as f:
                await f.write(raw_body)
            logger.info(
                f"Saved raw webhook request body ({len(raw_body)} bytes) to: {filepath}"
            )
        except Exception as e:
            logger.error(f"Failed to save raw webhook request body: {e}", exc_info=True)
        # --- End raw request saving ---

        form_data = await request.form()
        await store_incoming_email(db_context, dict(form_data))

        return Response(status_code=200, content="Email received.")
    except Exception as e:
        logger.error(f"Error processing mail webhook: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to process incoming email"
        ) from e
