import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

import aiofiles
from dateutil.parser import parse as parse_datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import AttachmentData, ParsedEmailData
from family_assistant.web.dependencies import get_db
from family_assistant.web.models import WebhookEventPayload

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.events.webhook_source import WebhookEventSource

logger = logging.getLogger(__name__)
webhooks_router = APIRouter()

# Default path if not found in config, though __main__ should set a default.
DEFAULT_ATTACHMENT_STORAGE_PATH = "/mnt/data/mailbox/attachments_fallback"
# Fallback for raw webhook dir if not in app config
DEFAULT_MAILBOX_RAW_DIR_FALLBACK = "/mnt/data/mailbox/raw_requests_fallback"


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

    # Determine directory for saving raw requests from app config or fallback

    mailbox_raw_dir_to_use: str = DEFAULT_MAILBOX_RAW_DIR_FALLBACK
    config: AppConfig | None = getattr(request.app.state, "config", None)
    if config and config.mailbox_raw_dir:
        mailbox_raw_dir_to_use = config.mailbox_raw_dir
    if mailbox_raw_dir_to_use == DEFAULT_MAILBOX_RAW_DIR_FALLBACK:
        logger.warning(
            f"mailbox_raw_dir not found in app.state.config, using fallback: {DEFAULT_MAILBOX_RAW_DIR_FALLBACK}"
        )

    try:
        os.makedirs(mailbox_raw_dir_to_use, exist_ok=True)
        now_dt = datetime.now(UTC)
        timestamp_str = now_dt.strftime("%Y%m%d_%H%M%S_%f")
        content_type_header = request.headers.get(
            "content-type", "unknown_content_type"
        )
        safe_content_type = (
            re.sub(r'[<>:"/\\|?*]', "_", content_type_header).split(";")[0].strip()
        )
        raw_filename = f"{timestamp_str}_{safe_content_type}.raw"
        raw_filepath = os.path.join(mailbox_raw_dir_to_use, raw_filename)

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
                    email_date_parsed = email_date_parsed.replace(tzinfo=UTC)
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

            # Get attachment storage path from app config
            attachment_storage_path = DEFAULT_ATTACHMENT_STORAGE_PATH
            app_config: AppConfig | None = getattr(request.app.state, "config", None)
            if app_config and app_config.attachment_storage_path:
                attachment_storage_path = app_config.attachment_storage_path

            base_attachment_dir = os.path.join(
                attachment_storage_path, email_attachment_batch_id
            )

            for i in range(1, attachment_count + 1):
                attachment_field_name = f"attachment-{i}"
                form_item = form_data.get(attachment_field_name)

                if isinstance(form_item, StarletteUploadFile) and form_item.filename:
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
                    detailed_reason = f"Type: {type(form_item)}"
                    if isinstance(form_item, StarletteUploadFile):
                        detailed_reason += f", Filename: '{form_item.filename}'"
                    else:
                        detailed_reason += f", Value: {str(form_item)[:100]}"  # Log first 100 chars if not UploadFile
                    logger.warning(
                        f"Skipping attachment field {attachment_field_name}: not a valid UploadFile with filename. Details: {detailed_reason}"
                    )

        # --- Create Pydantic Model ---
        # Convert form_data (FormData) to a plain dict for Pydantic parsing
        # FormData can have multiple values for a key, Pydantic expects single values or lists
        # For Mailgun, most fields are single value. message-headers is special (already handled).
        # We need to be careful if any other fields could be multi-valued.
        # For simplicity, assuming other relevant fields are single string values.
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        form_data_dict: dict[str, Any] = {
            key: form_data.get(key)
            for key in form_data  # type: ignore
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
        await db_context.email.store_incoming(parsed_email_payload)

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


class SMSWebhookPayload(BaseModel):
    """Payload for SMS webhook."""

    from_number: str = Field(alias="from")
    to_number: str = Field(alias="to")
    text: str


class WebhookEventResponse(BaseModel):
    """Response for webhook event endpoint."""

    status: str
    event_id: str


@webhooks_router.post("/webhook/sms")
async def handle_sms_webhook(
    request: Request,
    payload: SMSWebhookPayload,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> Response:
    """
    Receives incoming SMS via webhook from CrazyTel or similar.
    """
    logger.info(f"Received SMS webhook from {payload.from_number}")

    sms_service = getattr(request.app.state, "sms_service", None)
    if not sms_service:
        logger.error("SMSService not found in app.state")
        raise HTTPException(status_code=503, detail="SMS service not available")

    # Get chat_interfaces from app state for cross-interface messaging
    chat_interfaces = getattr(request.app.state, "chat_interfaces", None)

    try:
        await sms_service.handle_inbound_sms(
            db_context=db_context,
            from_number=payload.from_number,
            to_number=payload.to_number,
            text=payload.text,
            chat_interfaces=chat_interfaces,
        )
        return Response(status_code=200, content="SMS received")
    except Exception as e:
        logger.error(f"Error handling SMS webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@webhooks_router.post("/webhook/event")
async def handle_generic_webhook(
    request: Request,
    body: WebhookEventPayload,
    event_type: str | None = None,
    source: str | None = None,
) -> WebhookEventResponse:
    """
    Receives generic webhook events and routes them to the event processor.

    Events are matched against configured event listeners based on event_type,
    source, severity, and custom match conditions.

    Query parameters (optional, override body values):
        - event_type: Type/category of the event (useful for alertmanager webhooks)
        - source: Identifier for the event source

    Headers (optional):
        - X-Webhook-Signature: HMAC-SHA256 signature for verification
        - X-Webhook-Source: Alternative source identifier (overrides body and query source)

    Returns:
        JSON response with status and event_id
    """
    # Query params override body values
    effective_event_type = event_type or body.event_type
    if not effective_event_type:
        raise HTTPException(
            status_code=422,
            detail="event_type is required (provide in body or query parameter)",
        )

    # Determine source (header > query param > body)
    effective_source = request.headers.get("X-Webhook-Source") or source or body.source

    logger.info(
        f"Received webhook event: type={effective_event_type}, source={effective_source}"
    )

    # Get config for signature verification
    config: AppConfig | None = getattr(request.app.state, "config", None)

    # Verify signature if source has a configured secret
    if config and config.event_system.sources.webhook.secrets:
        source_secret = config.event_system.sources.webhook.secrets.get(
            effective_source or ""
        )
        if source_secret:
            signature = request.headers.get("X-Webhook-Signature")
            if not signature:
                raise HTTPException(
                    status_code=401,
                    detail=f"Signature required for source: {effective_source}",
                )

            # Compute expected signature
            raw_body = await request.body()
            expected = hmac.new(
                source_secret.encode(),
                raw_body,
                hashlib.sha256,
            ).hexdigest()
            expected_signature = f"sha256={expected}"

            if not hmac.compare_digest(expected_signature, signature):
                raise HTTPException(status_code=403, detail="Invalid signature")

    # Generate event ID
    event_id = str(uuid.uuid4())

    # Build event data for the processor
    # Extra fields first so system-generated values take precedence
    # ast-grep-ignore: no-dict-any - Event data intentionally combines webhook payload with generated fields
    event_data: dict[str, Any] = {
        **(body.model_extra or {}),  # Extra fields from payload (lowest priority)
        "event_id": event_id,
        "event_type": effective_event_type,
        "source": effective_source,
        "title": body.title,
        "message": body.message,
        "severity": body.severity,
        "data": body.data,
    }

    # Handle worker completion events - update task status in database
    if effective_event_type == "worker_completion":
        await _handle_worker_completion(request, body.data)

    # Get webhook source and emit event
    webhook_source: WebhookEventSource | None = getattr(
        request.app.state, "webhook_source", None
    )
    if not webhook_source:
        logger.warning("WebhookEventSource not configured, event will not be processed")
    else:
        await webhook_source.emit_event(event_data)

    return WebhookEventResponse(status="accepted", event_id=event_id)


async def _handle_worker_completion(
    request: Request,
    # ast-grep-ignore: no-dict-any - Webhook data is dynamic from external worker
    data: dict[str, Any] | None,
) -> None:
    """Handle worker completion webhook by updating task status.

    Args:
        request: The FastAPI request object
        data: The webhook data containing task_id, outcome, output, exit_code, callback_token
    """
    if not data:
        logger.warning("Worker completion event missing data payload")
        return

    task_id = data.get("task_id")
    if not task_id:
        logger.warning("Worker completion event missing task_id")
        return

    # Get database context
    db_context: DatabaseContext | None = getattr(request.app.state, "db_context", None)
    if not db_context:
        logger.error("DatabaseContext not available for worker completion handling")
        return

    # Verify callback token if the task has one stored
    task = await db_context.worker_tasks.get_task(task_id)
    if not task:
        logger.warning(f"Worker task {task_id} not found for completion update")
        return

    stored_token = task.get("callback_token")
    provided_token = data.get("callback_token")

    if stored_token:
        # Task has a stored token, must verify it
        if not provided_token:
            logger.warning(
                f"Worker completion for task {task_id} missing required callback_token"
            )
            return
        if not hmac.compare_digest(stored_token, provided_token):
            logger.warning(
                f"Worker completion for task {task_id} has invalid callback_token"
            )
            return
        logger.debug(f"Callback token verified for task {task_id}")

    outcome = data.get("outcome", "unknown")
    output = data.get("output")
    exit_code = data.get("exit_code")
    output_files = data.get("files", [])

    # Map outcome to status
    status_map = {
        "success": "success",
        "failure": "failed",
        "error": "failed",
        "timeout": "timeout",
        "cancelled": "cancelled",
    }
    status = status_map.get(outcome, "failed")

    try:
        # Update task status
        updated = await db_context.worker_tasks.update_task_status(
            task_id=task_id,
            status=status,
            completed_at=datetime.now(UTC),
            exit_code=exit_code,
            output_files=output_files,
            summary=output,
            error_message=output if status == "failed" else None,
        )

        if updated:
            logger.info(f"Updated worker task {task_id} status to {status}")
        else:
            logger.warning(f"Worker task {task_id} not found for completion update")

    except Exception as e:
        logger.error(f"Failed to update worker task {task_id}: {e}", exc_info=True)
