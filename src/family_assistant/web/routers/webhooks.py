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
from pydantic import BaseModel, ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from family_assistant.email_intake.actions import EMAIL_INTAKE_ACTION_TASK_TYPE
from family_assistant.email_intake.security import (
    EmailIntakePayloadTooLargeError,
    EmailIntakeSecurityError,
    enforce_attachment_size_limits,
    enforce_raw_request_size,
    extract_raw_mime,
    get_security_fields,
    verify_mailgun_signature,
    verify_sender_authorization,
)
from family_assistant.services.user_identity import (
    UserIdentityResolutionError,
    UserIdentityResolver,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import AttachmentData, ParsedEmailData
from family_assistant.web.dependencies import get_db
from family_assistant.web.models import WebhookEventPayload

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.events.webhook_source import WebhookEventSource

logger = logging.getLogger(__name__)
webhooks_router = APIRouter()


async def _save_raw_mail_webhook(
    *,
    raw_body_content: bytes,
    mailbox_raw_dir: str,
    content_type_header: str,
) -> None:
    """Save an accepted raw Mailgun webhook request for debugging/replay."""
    try:
        os.makedirs(mailbox_raw_dir, exist_ok=True)
        now_dt = datetime.now(UTC)
        timestamp_str = now_dt.strftime("%Y%m%d_%H%M%S_%f")
        safe_content_type = (
            re.sub(r'[<>:"/\\|?*]', "_", content_type_header).split(";")[0].strip()
        )
        raw_filename = f"{timestamp_str}_{safe_content_type}.raw"
        raw_filepath = os.path.join(mailbox_raw_dir, raw_filename)

        async with aiofiles.open(raw_filepath, "wb") as f:
            await f.write(raw_body_content)
        logger.info(
            f"Saved raw webhook request body ({len(raw_body_content)} bytes) to: {raw_filepath}"
        )
    except Exception as e:
        logger.error(f"Failed to save raw webhook request body: {e}", exc_info=True)


@webhooks_router.post("/webhook/mail")
@webhooks_router.post("/webhook/mail/mime")
async def handle_mail_webhook(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> Response:
    """
    Receives incoming email via webhook (expects multipart/form-data from Mailgun),
    parses it, saves attachments, and passes structured data to the storage layer.

    Mailgun only includes the raw RFC 822 message in the ``body-mime`` form field when
    the route's Destination URL path ends in ``mime`` or ``raw-mime``. The alias route
    ``/webhook/mail/mime`` exists so operators can point Mailgun at a URL that satisfies
    that suffix requirement without renaming the legacy ``/webhook/mail`` path.
    """
    logger.info("Received POST request on /webhook/mail")

    # ``Assistant.setup_dependencies()`` attaches ``config`` to
    # ``app.state`` before the HTTP server starts accepting traffic. A
    # request arriving without it is a boot-order / misconfiguration bug:
    # reject it outright rather than silently accepting it under
    # defaults, which would bypass Mailgun signature verification
    # (no signing key configured) and persist attachments under a
    # directory the runtime registry doesn't know about.
    config: AppConfig | None = getattr(request.app.state, "config", None)
    if config is None:
        logger.error(
            "/webhook/mail received a request before app.state.config "
            "was attached; rejecting with 503."
        )
        raise HTTPException(
            status_code=503,
            detail="Email webhook not ready: application config unavailable",
        )
    email_intake_config = config.email_intake
    # ``mailbox_raw_dir`` is optional — it only drives raw-request
    # archiving for debugging/replay. When unset we skip the archive
    # step but still accept the email; the core intake path doesn't
    # depend on it.
    mailbox_raw_dir_to_use: str | None = config.mailbox_raw_dir
    # ``attachment_storage_path`` is only needed when the email has
    # attachments — the check is deferred into the attachment loop
    # below so attachment-free mail still gets accepted even in the
    # unusual case where this field is unset.
    attachment_storage_path: str | None = config.attachment_storage_path

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            content_length_bytes = int(content_length)
        except ValueError:
            logger.warning("Ignoring invalid Content-Length header: %s", content_length)
        else:
            if content_length_bytes > email_intake_config.max_raw_request_bytes:
                msg = (
                    "Inbound email webhook payload exceeds configured limit "
                    f"({content_length_bytes} > "
                    f"{email_intake_config.max_raw_request_bytes} bytes)"
                )
                logger.warning("Rejecting oversized inbound email webhook: %s", msg)
                raise HTTPException(status_code=413, detail=msg)

    raw_body_content = await request.body()

    try:
        enforce_raw_request_size(raw_body_content, email_intake_config)
    except EmailIntakePayloadTooLargeError as exc:
        logger.warning("Rejecting oversized inbound email webhook: %s", exc)
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    try:
        # FastAPI's request.form() will parse multipart/form-data
        form_data = await request.form()

        timestamp, token, signature = get_security_fields(form_data)
        verify_mailgun_signature(
            timestamp=timestamp,
            token=token,
            signature=signature,
            config=email_intake_config,
        )
        raw_mime = await extract_raw_mime(form_data)
        if raw_mime is None and not email_intake_config.require_authenticated_sender:
            logger.warning(
                "Inbound email webhook did not include body-mime; DKIM/DMARC "
                "verification skipped. Configure the Mailgun route to use MIME "
                "forwarding before enabling require_authenticated_sender."
            )
        dns_resolver = getattr(request.app.state, "email_intake_dns_resolver", None)
        authentication = verify_sender_authorization(
            form_data,
            email_intake_config,
            raw_mime=raw_mime,
            dns_resolver=dns_resolver,
        )
        if authentication is not None:
            logger.info(
                "Inbound email authentication: dkim=%s spf=%s dmarc=%s "
                "from_domain=%s dkim_domain=%s",
                authentication.dkim,
                authentication.spf,
                authentication.dmarc,
                authentication.from_domain,
                authentication.dkim_domain,
            )
        user_identity_resolver = getattr(
            request.app.state, "user_identity_resolver", None
        )
        if user_identity_resolver is None:
            user_identity_resolver = UserIdentityResolver(config)
            request.app.state.user_identity_resolver = user_identity_resolver
        try:
            target_user_id = user_identity_resolver.resolve_email_intake_user(form_data)
        except UserIdentityResolutionError as exc:
            raise EmailIntakeSecurityError(str(exc)) from exc
        if mailbox_raw_dir_to_use is not None:
            await _save_raw_mail_webhook(
                raw_body_content=raw_body_content,
                mailbox_raw_dir=mailbox_raw_dir_to_use,
                content_type_header=request.headers.get(
                    "content-type", "unknown_content_type"
                ),
            )

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
        # The webhook saves attachments to disk and records their metadata on
        # the email row. Registration with ``AttachmentRegistry`` happens later
        # in ``EmailIndexer.handle_index_email`` — a single-writer write path
        # that avoids race conditions.
        processed_attachments: list[AttachmentData] = []
        attachment_count_str = form_data.get("attachment-count")
        if (
            isinstance(attachment_count_str, str)
            and attachment_count_str.isdigit()
            and int(attachment_count_str) > 0
        ):
            if not attachment_storage_path:
                # Only required when the email has attachments; attachment-
                # free mail goes through without needing a mailbox dir.
                logger.error(
                    "/webhook/mail: attachment-count=%s but "
                    "config.attachment_storage_path is not set; refusing "
                    "to accept inbound email with attachments without a "
                    "configured attachment directory.",
                    attachment_count_str,
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Email webhook not ready: attachment_storage_path "
                        "unconfigured but email has attachments"
                    ),
                )
            attachment_count = int(attachment_count_str)
            # Generate a single UUID for this email's attachments directory
            email_attachment_batch_id = str(uuid.uuid4())
            total_attachment_size = 0

            # On-disk write location for the batch: resolved against the
            # configured mailbox base (validated non-empty just above).
            # We persist a path *relative* to that base so environment
            # moves (mounts, restores) stay portable:
            # ``AttachmentRegistry.get_attachment_path`` rejoins the
            # relative path against ``email_attachment_base_path`` at
            # read time.
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
                        # Prefix the saved filename with the attachment index
                        # so that two parts sharing the same filename don't
                        # overwrite each other on disk and don't collapse to
                        # the same email-attachment dedup key
                        # (message_id, storage_path).
                        persisted_filename = f"{i}-{safe_filename}"
                        # Disk I/O happens at the absolute path; the
                        # registry row stores the relative path so
                        # environment moves (mounts, restores) stay
                        # portable. ``AttachmentRegistry`` rejoins it
                        # against ``email_attachment_base_path`` at read
                        # time.
                        disk_path = os.path.join(
                            base_attachment_dir, persisted_filename
                        )
                        persisted_storage_path = os.path.join(
                            email_attachment_batch_id, persisted_filename
                        )

                        # Save the uploaded file
                        await form_item.seek(0)  # Ensure pointer is at the start
                        content = await form_item.read()
                        size = len(content)
                        total_attachment_size += size
                        enforce_attachment_size_limits(
                            attachment_name=safe_filename,
                            attachment_size=size,
                            total_attachment_size=total_attachment_size,
                            config=email_intake_config,
                        )
                        async with aiofiles.open(disk_path, "wb") as f_out:
                            await f_out.write(content)

                        attachment_mime_type = (
                            form_item.content_type or "application/octet-stream"
                        )
                        processed_attachments.append(
                            AttachmentData(
                                filename=safe_filename,
                                content_type=attachment_mime_type,
                                size=size,
                                storage_path=persisted_storage_path,
                            )
                        )
                        logger.info(
                            f"Saved attachment '{safe_filename}' to {disk_path} "
                            f"(stored path: {persisted_storage_path})"
                        )
                    except EmailIntakePayloadTooLargeError:
                        raise
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
        # ast-grep-ignore: no-dict-any - Mailgun form data has dynamic fields that vary by email
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
            target_user_id=target_user_id,
            dkim_result=authentication.dkim if authentication else None,
            spf_result=authentication.spf if authentication else None,
            dmarc_result=authentication.dmarc if authentication else None,
            dmarc_policy=authentication.dmarc_policy if authentication else None,
            dkim_domain=authentication.dkim_domain if authentication else None,
        )

        # Pass the Pydantic model instance to the storage function
        email_db_id = await db_context.email.store_incoming(parsed_email_payload)
        if (
            email_db_id is not None
            and email_intake_config.enable_actions
            and target_user_id is not None
        ):
            await db_context.tasks.enqueue(
                task_id=f"email_intake_action_{email_db_id}",
                task_type=EMAIL_INTAKE_ACTION_TASK_TYPE,
                payload={
                    "email_db_id": email_db_id,
                    "interface_type": "email",
                    "conversation_id": f"email:{email_db_id}",
                    "user_name": target_user_id,
                },
                original_task_id=f"email_intake_action_{email_db_id}",
                max_retries_override=0,
            )

        return Response(status_code=200, content="Email received and processed.")

    except EmailIntakePayloadTooLargeError as exc:
        logger.warning("Rejecting oversized inbound email webhook: %s", exc)
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except EmailIntakeSecurityError as exc:
        logger.warning("Rejecting unauthorized inbound email webhook: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
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


class WebhookEventResponse(BaseModel):
    """Response for webhook event endpoint."""

    status: str
    event_id: str


@webhooks_router.post("/webhook/event")
async def handle_generic_webhook(
    request: Request,
    body: WebhookEventPayload,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
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
        await _handle_worker_completion(db_context, body.data)

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
    db_context: DatabaseContext,
    # ast-grep-ignore: no-dict-any - Webhook data is dynamic from external worker
    data: dict[str, Any] | None,
) -> None:
    """Handle worker completion webhook by updating task status.

    Args:
        db_context: Database context for data access
        data: The webhook data containing task_id, outcome, output, exit_code, callback_token
    """
    if not data:
        logger.warning("Worker completion event missing data payload")
        return

    task_id = data.get("task_id")
    if not task_id:
        logger.warning("Worker completion event missing task_id")
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
