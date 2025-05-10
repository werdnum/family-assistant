import json
import logging
import os
import re
import uuid  # For generating unique IDs for attachment paths
from datetime import datetime, timezone
from typing import Annotated, Any

import aiofiles
from dateutil.parser import parse as parse_datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from pydantic import ValidationError

from family_assistant.storage import store_incoming_email
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import AttachmentData, ParsedEmailData
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)
webhooks_router = APIRouter()

# Directory to save raw webhook request bodies for debugging/replay
MAILBOX_RAW_DIR = os.getenv("MAILBOX_RAW_DIR", "/mnt/data/mailbox/raw_requests")

# Directory for storing processed attachments
ATTACHMENT_STORAGE_DIR = os.getenv(
    "ATTACHMENT_STORAGE_DIR", "/mnt/data/mailbox/attachments"
)


@webhooks_router.post("/webhook/mail")
async def handle_mail_webhook(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> Response:
    """
    Receives incoming email via webhook (expects multipart/form-data from Mailgun),
    parses it, saves attachments, and passes structured data to the storage layer.
    """
    logger.info("Received POST request on /webhook/mail")
    # --- Save raw request body for debugging/replay ---
    # It's good to read the body once if needed for both raw saving and form parsing.
    # However, request.form() consumes the body. If raw saving is critical,
    # read body first, then pass to a method that can parse from bytes if FastAPI allows,
    # or accept that form() is the primary way. For now, keeping existing raw save logic.
    raw_body_content = await request.body()
    try:
        os.makedirs(MAILBOX_RAW_DIR, exist_ok=True)
        now_dt = datetime.now(timezone.utc)
        timestamp_str = now_dt.strftime("%Y%m%d_%H%M%S_%f")
        content_type_header = request.headers.get(
            "content-type", "unknown_content_type"
        )
        safe_content_type = (
            re.sub(r'[<>:"/\\|?*]', "_", content_type_header).split(";")[0].strip()
        )
        raw_filename = f"{timestamp_str}_{safe_content_type}.raw"
        raw_filepath = os.path.join(MAILBOX_RAW_DIR, raw_filename)

        async with aiofiles.open(raw_filepath, "wb") as f:
            await f.write(raw_body_content)
        logger.info(
            f"Saved raw webhook request body ({len(raw_body_content)} bytes) to: {raw_filepath}"
        )
    except Exception as e:
        logger.error(f"Failed to save raw webhook request body: {e}", exc_info=True)
    # --- End raw request saving ---

    try:
        # FastAPI's request.form() will parse multipart/form-data
        form_data = await request.form()

        # --- Parse Email Date ---
        email_date_parsed: datetime | None = None
        email_date_str = form_data.get("Date")
        if isinstance(email_date_str, str):
            try:
                email_date_parsed = parse_datetime(email_date_str)
                if email_date_parsed.tzinfo is None:
                    email_date_parsed = email_date_parsed.replace(tzinfo=timezone.utc)
            except Exception as e:
                logger.warning(
                    f"Could not parse email Date header '{email_date_str}': {e}"
                )

        # --- Parse Headers ---
        headers_list: list[list[str]] | None = None
        headers_raw = form_data.get("message-headers")
        if isinstance(headers_raw, str):
            try:
                headers_list = json.loads(headers_raw)
            except json.JSONDecodeError as e:
                logger.warning(f"Could not decode message-headers JSON: {e}")

        # --- Process Attachments ---
        processed_attachments: list[AttachmentData] = []
        attachment_count_str = form_data.get("attachment-count")
        if isinstance(attachment_count_str, str) and attachment_count_str.isdigit():
            attachment_count = int(attachment_count_str)
            # Generate a single UUID for this email's attachments directory
            email_attachment_batch_id = str(uuid.uuid4())
            base_attachment_dir = os.path.join(
                ATTACHMENT_STORAGE_DIR, email_attachment_batch_id
            )

            for i in range(1, attachment_count + 1):
                attachment_field_name = f"attachment-{i}"
                form_item = form_data.get(attachment_field_name)

                if isinstance(form_item, UploadFile) and form_item.filename:
                    try:
                        os.makedirs(base_attachment_dir, exist_ok=True)
                        # Sanitize filename (basic)
                        safe_filename = os.path.basename(form_item.filename)
                        final_file_path = os.path.join(
                            base_attachment_dir, safe_filename
                        )

                        # Save the uploaded file
                        await form_item.seek(0)  # Ensure pointer is at the start
                        async with aiofiles.open(final_file_path, "wb") as f_out:
                            content = await form_item.read()
                            await f_out.write(content)

                        # Get size after reading
                        size = (
                            form_item.size
                            if form_item.size is not None
                            else len(content)
                        )

                        processed_attachments.append(
                            AttachmentData(
                                filename=safe_filename,
                                content_type=form_item.content_type
                                or "application/octet-stream",
                                size=size,
                                storage_path=final_file_path,
                            )
                        )
                        logger.info(
                            f"Saved attachment '{safe_filename}' to {final_file_path}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to save attachment {form_item.filename}: {e}",
                            exc_info=True,
                        )
                    finally:
                        await form_item.close()  # Close the upload file
                elif form_item:  # Not an UploadFile or no filename
                    logger.warning(
                        f"Skipping attachment field {attachment_field_name}: not a valid UploadFile with filename."
                    )

        # --- Create Pydantic Model ---
        # Convert form_data (FormData) to a plain dict for Pydantic parsing
        # FormData can have multiple values for a key, Pydantic expects single values or lists
        # For Mailgun, most fields are single value. message-headers is special (already handled).
        # We need to be careful if any other fields could be multi-valued.
        # For simplicity, assuming other relevant fields are single string values.
        form_data_dict: dict[str, Any] = {
            key: form_data.get(key) for key in form_data  # type: ignore
        }

        parsed_email_payload = ParsedEmailData(
            **form_data_dict,  # Pass all form fields, Pydantic will pick what it needs by alias
            email_date=email_date_parsed,  # Override with parsed version
            headers_json=headers_list,  # Override with parsed version
            attachment_info=(
                processed_attachments if processed_attachments else None
            ),  # Override
        )

        # Pass the Pydantic model instance to the storage function
        await store_incoming_email(db_context, parsed_email_payload)

        return Response(status_code=200, content="Email received and processed.")

    except ValidationError as ve:
        logger.error(
            f"Pydantic validation error processing mail webhook: {ve.errors()}",
            exc_info=True,
        )
        # Log ve.json() for more details if needed
        raise HTTPException(
            status_code=422, detail=f"Invalid email data: {ve.errors()}"
        ) from ve
    except Exception as e:
        logger.error(f"Error processing mail webhook: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to process incoming email"
        ) from e
