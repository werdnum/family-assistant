from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import telegramify_markdown
from PIL import Image
from telegram import ForceReply, InputMediaPhoto
from telegram.constants import ParseMode

from family_assistant.interfaces import ChatInterface
from family_assistant.storage.context import DatabaseContext

if TYPE_CHECKING:
    from telegram.ext import Application

    from family_assistant.services.attachment_registry import AttachmentRegistry


logger = logging.getLogger(__name__)

TELEGRAM_PHOTO_SIZE_LIMIT = 10 * 1024 * 1024  # 10MB


class TelegramChatInterface(ChatInterface):
    """
    Implementation of ChatInterface for Telegram.
    Uses an underlying telegram.ext.Application instance to send messages.
    """

    def __init__(
        self,
        application: Application,
        attachment_registry: AttachmentRegistry | None = None,
    ) -> None:
        """
        Initializes the TelegramChatInterface.

        Args:
            application: The telegram.ext.Application instance.
            attachment_registry: The AttachmentRegistry for handling file attachments.
        """
        self.application = application
        self.attachment_registry = attachment_registry

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        """
        Sends a message to the specified Telegram chat.

        Args:
            conversation_id: The Telegram chat_id (as a string).
            text: The message text to send.
            parse_mode: Optional string indicating the formatting mode ("MarkdownV2", "HTML").
            reply_to_interface_id: Optional Telegram message_id (as a string) to reply to.
            attachment_ids: Optional list of attachment IDs to send with the message.

        Returns:
            The Telegram message_id of the sent message as a string, or None if sending failed.
        """
        tg_parse_mode: ParseMode | None = None
        if parse_mode == "MarkdownV2":
            tg_parse_mode = ParseMode.MARKDOWN_V2
        elif parse_mode == "HTML":
            tg_parse_mode = ParseMode.HTML
        elif parse_mode is not None:
            logger.warning(
                f"Unsupported parse_mode '{parse_mode}' for Telegram. Sending as plain text."
            )

        text_to_send = text
        final_parse_mode = tg_parse_mode

        if final_parse_mode == ParseMode.MARKDOWN_V2:
            try:
                text_to_send = telegramify_markdown.markdownify(text)
            except Exception as md_err:
                logger.error(
                    f"Failed to convert text to MarkdownV2 for chat {conversation_id}: {md_err}. Sending as plain text.",
                    exc_info=True,
                )
                text_to_send = text
                final_parse_mode = None

        try:
            chat_id_int = int(conversation_id)
            reply_to_msg_id_int = (
                int(reply_to_interface_id) if reply_to_interface_id else None
            )

            force_reply_markup = ForceReply(selective=False)

            if attachment_ids:
                await self._send_attachments(
                    chat_id_int, attachment_ids, reply_to_msg_id_int
                )

            sent_msg = await self.application.bot.send_message(
                chat_id=chat_id_int,
                text=text_to_send,
                parse_mode=final_parse_mode,
                reply_to_message_id=reply_to_msg_id_int,
                reply_markup=force_reply_markup,
            )

            return str(sent_msg.message_id)
        except ValueError:
            logger.error(
                f"Invalid conversation_id '{conversation_id}' or reply_to_interface_id '{reply_to_interface_id}' for Telegram. Must be integer convertible."
            )
            return None
        except Exception as e:
            logger.error(
                f"TelegramChatInterface failed to send message to {conversation_id}: {e}",
                exc_info=True,
            )
            return None

    def _resize_image_if_needed(
        self, content: bytes, attachment_id: str
    ) -> tuple[bytes, str | None]:
        """
        Resize image if it exceeds Telegram's photo size limit.

        Uses PIL to intelligently resize large images to fit within Telegram's 10MB photo limit
        while preserving aspect ratio and quality. Targets ~20 megapixels which typically
        results in ~8MB JPEG files with good quality.

        Args:
            content: Original image content as bytes
            attachment_id: ID of the attachment for logging

        Returns:
            Tuple of (processed_content, size_note):
            - processed_content: Resized image bytes if needed, or original if small enough
            - size_note: Caption text with link to full resolution if resized, None otherwise
        """
        if len(content) <= TELEGRAM_PHOTO_SIZE_LIMIT:
            return content, None

        logger.info(
            f"Image {attachment_id} is {len(content) / (1024 * 1024):.1f}MB, "
            f"resizing to fit {TELEGRAM_PHOTO_SIZE_LIMIT / (1024 * 1024):.0f}MB limit"
        )

        try:
            TARGET_MEGAPIXELS = 20

            with Image.open(io.BytesIO(content)) as img:
                original_width, original_height = img.size
                original_megapixels = (original_width * original_height) / 1_000_000

                if original_megapixels <= TARGET_MEGAPIXELS:
                    scale_factor = 1.0
                else:
                    scale_factor = (TARGET_MEGAPIXELS / original_megapixels) ** 0.5

                new_width = int(original_width * scale_factor)
                new_height = int(original_height * scale_factor)

                img_rgb = img.convert("RGB") if img.mode != "RGB" else img

                if scale_factor < 1.0:
                    resized_img = img_rgb.resize(
                        (new_width, new_height), Image.Resampling.LANCZOS
                    )
                else:
                    resized_img = img_rgb

                output_buffer = io.BytesIO()
                resized_img.save(
                    output_buffer, format="JPEG", quality=85, optimize=True
                )
                resized_content = output_buffer.getvalue()

                if len(resized_content) > TELEGRAM_PHOTO_SIZE_LIMIT:
                    logger.error(
                        f"Resized image {attachment_id} is still {len(resized_content) / (1024 * 1024):.1f}MB, "
                        f"exceeds {TELEGRAM_PHOTO_SIZE_LIMIT / (1024 * 1024):.0f}MB limit"
                    )
                    return content, None

                logger.info(
                    f"Resized image {attachment_id} from {len(content) / (1024 * 1024):.1f}MB "
                    f"to {len(resized_content) / (1024 * 1024):.1f}MB "
                    f"({original_width}x{original_height} -> {new_width}x{new_height})"
                )

                size_note = f"[Full resolution: /attachment {attachment_id}]"
                return resized_content, size_note

        except Exception as e:
            logger.error(f"Failed to resize image {attachment_id}: {e}", exc_info=True)
            return content, None

    async def _send_attachments(
        self,
        chat_id: int,
        attachment_ids: list[str],
        reply_to_msg_id: int | None = None,
    ) -> list[str]:
        """
        Send attachments to a Telegram chat.

        Groups consecutive image attachments into media groups when there are multiple.
        Non-image attachments are sent individually as documents.

        Args:
            chat_id: The Telegram chat ID.
            attachment_ids: List of attachment IDs to send.
            reply_to_msg_id: Optional message ID to reply to.

        Returns:
            List of message IDs for sent attachments.
        """
        message_ids = []

        if not self.attachment_registry:
            logger.warning(
                f"TelegramChatInterface: Cannot send {len(attachment_ids)} attachments - "
                "AttachmentRegistry not available."
            )
            return message_ids

        try:
            async with DatabaseContext(
                self.attachment_registry.db_engine
            ) as db_context:
                attachments_data = []
                for attachment_id in attachment_ids:
                    try:
                        metadata = await self.attachment_registry.get_attachment(
                            db_context, attachment_id
                        )
                        if not metadata:
                            logger.warning(f"Attachment {attachment_id} not found")
                            continue

                        content = await self.attachment_registry.get_attachment_content(
                            db_context, attachment_id
                        )
                        if not content:
                            logger.warning(
                                f"Content for attachment {attachment_id} not found"
                            )
                            continue

                        attachments_data.append({
                            "id": attachment_id,
                            "metadata": metadata,
                            "content": content,
                        })
                    except Exception as e:
                        logger.error(
                            f"Error fetching attachment {attachment_id}: {e}",
                            exc_info=True,
                        )
                        continue

                i = 0
                while i < len(attachments_data):
                    attachment = attachments_data[i]
                    content_type = attachment["metadata"].mime_type or ""

                    if content_type.startswith("image/"):
                        image_group = [attachment]
                        j = i + 1
                        while j < len(attachments_data):
                            next_attachment = attachments_data[j]
                            next_content_type = (
                                next_attachment["metadata"].mime_type or ""
                            )
                            if next_content_type.startswith("image/"):
                                image_group.append(next_attachment)
                                j += 1
                            else:
                                break

                        if len(image_group) > 1:
                            media_group = []
                            resize_notes = []
                            oversized_attachments = []

                            for img_data in image_group:
                                processed_content, size_note = (
                                    self._resize_image_if_needed(
                                        img_data["content"],
                                        img_data["id"],
                                    )
                                )

                                if size_note:
                                    resize_notes.append(size_note)

                                if len(processed_content) > TELEGRAM_PHOTO_SIZE_LIMIT:
                                    oversized_attachments.append(img_data)
                                    continue

                                media_group.append(
                                    InputMediaPhoto(media=io.BytesIO(processed_content))
                                )

                            caption_parts = []
                            first_filename = (
                                image_group[0]["metadata"].description
                                or f"attachment_{image_group[0]['id']}"
                            )
                            caption_parts.append(first_filename)

                            if resize_notes:
                                caption_parts.extend(resize_notes)

                            caption = "\n".join(caption_parts)[:1024]

                            if media_group:
                                sent_messages = (
                                    await self.application.bot.send_media_group(
                                        chat_id=chat_id,
                                        media=media_group,
                                        reply_to_message_id=reply_to_msg_id,
                                        caption=caption,
                                    )
                                )
                                for sent_msg in sent_messages:
                                    message_ids.append(str(sent_msg.message_id))

                                logger.info(
                                    f"Sent media group with {len(media_group)} images: "
                                    f"{[img['id'] for img in image_group if img not in oversized_attachments]}"
                                )

                            for img_data in oversized_attachments:
                                filename = (
                                    img_data["metadata"].description
                                    or f"attachment_{img_data['id']}"
                                )
                                sent_msg = await self.application.bot.send_document(
                                    chat_id=chat_id,
                                    document=io.BytesIO(img_data["content"]),
                                    filename=filename,
                                    caption=f"Image too large to send as photo (>10MB)\n[View: /attachment {img_data['id']}]",
                                    reply_to_message_id=reply_to_msg_id,
                                )
                                message_ids.append(str(sent_msg.message_id))
                                logger.warning(
                                    f"Sent oversized image {img_data['id']} as document"
                                )

                        else:
                            img_data = image_group[0]
                            processed_content, size_note = self._resize_image_if_needed(
                                img_data["content"], img_data["id"]
                            )

                            caption_parts = []
                            filename = (
                                img_data["metadata"].description
                                or f"attachment_{img_data['id']}"
                            )
                            caption_parts.append(filename)

                            if size_note:
                                caption_parts.append(size_note)

                            caption = "\n".join(caption_parts)[:1024]

                            if len(processed_content) > TELEGRAM_PHOTO_SIZE_LIMIT:
                                filename = (
                                    img_data["metadata"].description
                                    or f"attachment_{img_data['id']}"
                                )
                                sent_msg = await self.application.bot.send_document(
                                    chat_id=chat_id,
                                    document=io.BytesIO(img_data["content"]),
                                    filename=filename,
                                    caption=f"Image too large to send as photo (>10MB)\n[View: /attachment {img_data['id']}]",
                                    reply_to_message_id=reply_to_msg_id,
                                )
                                message_ids.append(str(sent_msg.message_id))
                                logger.warning(
                                    f"Sent oversized image {img_data['id']} as document"
                                )
                            else:
                                sent_msg = await self.application.bot.send_photo(
                                    chat_id=chat_id,
                                    photo=io.BytesIO(processed_content),
                                    caption=caption,
                                    reply_to_message_id=reply_to_msg_id,
                                )
                                message_ids.append(str(sent_msg.message_id))
                                logger.info(
                                    f"Sent image attachment {img_data['id']} as message {sent_msg.message_id}"
                                )

                        i = j
                    else:
                        filename = (
                            attachment["metadata"].description
                            or f"attachment_{attachment['id']}"
                        )
                        sent_msg = await self.application.bot.send_document(
                            chat_id=chat_id,
                            document=io.BytesIO(attachment["content"]),
                            filename=filename,
                            reply_to_message_id=reply_to_msg_id,
                        )
                        message_ids.append(str(sent_msg.message_id))
                        logger.info(
                            f"Sent document attachment {attachment['id']} as message {sent_msg.message_id}"
                        )
                        i += 1

        except Exception as e:
            logger.error(f"Error in _send_attachments: {e}", exc_info=True)

        return message_ids