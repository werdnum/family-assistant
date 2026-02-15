"""
Direct Anthropic API implementation for LLM interactions.
"""

import base64
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

import aiofiles
import anthropic

from family_assistant.llm import (
    BaseLLMClient,
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPart,
    ImageUrlContentPart,
    LLMMessage,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
    message_to_json_dict,
)
from family_assistant.llm.request_buffer import LLMRequestRecord, get_request_buffer

from ..base import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    LLMProviderError,
    ModelNotFoundError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)

StreamingMetadata = dict[str, object]


logger = logging.getLogger(__name__)


class AnthropicClient(BaseLLMClient):
    """Direct Anthropic API implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        model_parameters: dict[str, dict[str, object]] | None = None,
        **kwargs: Any,  # noqa: ANN401 # Accepts arbitrary Anthropic API parameters
    ) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.model_parameters = model_parameters or {}
        self.default_kwargs = kwargs
        logger.info(
            f"AnthropicClient initialized for model: {model} with default kwargs: {kwargs}, "
            f"model-specific parameters: {model_parameters}"
        )

    def _supports_multimodal_tools(self) -> bool:
        """Anthropic supports images and PDFs in tool results natively."""
        return True

    def _process_tool_messages(
        self,
        messages: list[LLMMessage],
    ) -> list[LLMMessage]:
        """Process tool messages, converting attachments to native Anthropic format.

        Anthropic supports images and PDFs natively in tool results via `image`
        and `document` content blocks. Unsupported attachment types fall back to
        base class text injection.
        """
        if not self._supports_multimodal_tools():
            return super()._process_tool_messages(messages)

        processed: list[LLMMessage] = []
        for original_msg in messages:
            if (
                isinstance(original_msg, ToolMessage)
                and original_msg.transient_attachments
            ):
                attachments = original_msg.transient_attachments
                # ast-grep-ignore: no-dict-any - Anthropic content block format
                content: list[dict[str, Any]] = [
                    {"type": "text", "text": original_msg.content},
                ]
                injection_msgs: list[LLMMessage] = []
                for attachment in attachments:
                    if attachment.content and attachment.mime_type.startswith("image/"):
                        b64_data = attachment.get_content_as_base64()
                        if b64_data:
                            content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": attachment.mime_type,
                                    "data": b64_data,
                                },
                            })
                    elif (
                        attachment.content and attachment.mime_type == "application/pdf"
                    ):
                        b64_data = attachment.get_content_as_base64()
                        if b64_data:
                            content.append({
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": attachment.mime_type,
                                    "data": b64_data,
                                },
                            })
                    elif attachment.content or attachment.file_path:
                        if attachment.content:
                            logger.warning(
                                f"Unsupported attachment type {attachment.mime_type} for Anthropic, falling back to text description"
                            )
                        else:
                            logger.warning(
                                f"File-path-only attachment {attachment.file_path} for Anthropic, falling back to text description"
                            )
                        content[0]["text"] += "\n[File content in following message]"
                        injection_msg = self.create_attachment_injection(attachment)
                        injection_msgs.append(injection_msg)
                updated_msg = original_msg.model_copy(
                    update={
                        "content": content,
                        "transient_attachments": None,
                    }
                )
                processed.append(updated_msg)
                if injection_msgs:
                    processed.extend(injection_msgs)
            else:
                processed.append(original_msg)
        return processed

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> UserMessage:
        """Create user message with attachment for Anthropic."""
        # Handle JSON/text attachments using base class logic first
        if (
            attachment.content
            and attachment.mime_type
            and (
                attachment.mime_type in {"application/json", "text/csv"}
                or attachment.mime_type.startswith("text/")
            )
        ):
            return super().create_attachment_injection(attachment)

        # Handle multimodal content (images) with typed content parts
        content_parts: list[ContentPart] = [
            TextContentPart(
                type="text", text="[System: File from previous tool response]"
            )
        ]

        if attachment.content and attachment.mime_type.startswith("image/"):
            b64_data = attachment.get_content_as_base64()
            if b64_data:
                content_parts.append(
                    ImageUrlContentPart(
                        type="image_url",
                        image_url={
                            "url": f"data:{attachment.mime_type};base64,{b64_data}"
                        },
                    )
                )
        elif attachment.content and attachment.mime_type == "application/pdf":
            size_mb = len(attachment.content) / (1024 * 1024)
            content_parts.append(
                TextContentPart(
                    type="text",
                    text=f"[PDF Document: {attachment.description or 'document.pdf'} "
                    f"({size_mb:.1f}MB) - Content cannot be displayed but was provided "
                    f"as context from the previous tool response]",
                )
            )
        elif attachment.content:
            size_mb = len(attachment.content) / (1024 * 1024)
            content_parts.append(
                TextContentPart(
                    type="text",
                    text=f"[File content: {attachment.mime_type}, {size_mb:.1f}MB - {attachment.description}. Note: Binary content not accessible to model, text extraction may be needed]",
                )
            )
        elif attachment.file_path:
            content_parts.append(
                TextContentPart(
                    type="text",
                    text=f"[File: {attachment.file_path} - Note: File content not accessible to model]",
                )
            )

        return UserMessage(content=content_parts)

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def _get_model_specific_params(self, model: str) -> dict[str, object]:
        """Get parameters for a specific model based on pattern matching."""
        params = {}
        for pattern, pattern_params in self.model_parameters.items():
            if pattern in model:
                params.update(pattern_params)
                logger.debug(
                    f"Applied parameters for pattern '{pattern}': {pattern_params}"
                )
        return params

    @staticmethod
    def _convert_tools_to_anthropic_format(
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, object]],
        # ast-grep-ignore: no-dict-any - Anthropic tool format
    ) -> list[dict[str, object]]:
        """Convert OpenAI-style tool definitions to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            func = tool.get("function", {})
            if not isinstance(func, dict):
                continue
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            })
        return anthropic_tools

    @staticmethod
    def _convert_tool_choice_to_anthropic(
        tool_choice: str | None,
        # ast-grep-ignore: no-dict-any - Anthropic tool_choice format
    ) -> dict[str, str] | None:
        """Convert tool_choice string to Anthropic format."""
        if tool_choice is None or tool_choice == "none":
            return None
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice in {"required", "any"}:
            return {"type": "any"}
        # Specific tool name
        return {"type": "tool", "name": tool_choice}

    def _convert_messages_to_anthropic_format(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Anthropic message format
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert typed messages to Anthropic API format.

        Returns (system_prompt, api_messages).

        Key differences from OpenAI:
        - System messages are extracted to the top-level `system` parameter
        - Tool results use role: "user" with tool_result content blocks
        - Assistant tool calls use tool_use content blocks
        - Consecutive same-role messages are merged (Anthropic requires alternating roles)
        - Images use source.type: "base64" format
        """
        system_parts: list[str] = []
        # ast-grep-ignore: no-dict-any - Anthropic message format
        api_messages: list[dict[str, Any]] = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                system_parts.append(msg.content)

            elif isinstance(msg, UserMessage):
                content = self._convert_user_content(msg)
                api_messages.append({"role": "user", "content": content})

            elif isinstance(msg, AssistantMessage):
                # ast-grep-ignore: no-dict-any - Anthropic content block format
                content_blocks: list[dict[str, Any]] = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        arguments = tc.function.arguments
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.function.name,
                            "input": arguments,
                        })
                if content_blocks:
                    api_messages.append({
                        "role": "assistant",
                        "content": content_blocks,
                    })

            elif isinstance(msg, ToolMessage):
                # content may be a string or list of content blocks (from _process_tool_messages
                # for native multimodal support). Anthropic tool_result accepts both.
                # ast-grep-ignore: no-dict-any - Anthropic tool_result content block format
                tool_result_block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }
                api_messages.append({"role": "user", "content": [tool_result_block]})

        # Merge consecutive same-role messages (Anthropic requires alternating roles)
        api_messages = self._merge_consecutive_roles(api_messages)

        system_prompt = "\n\n".join(system_parts) if system_parts else None
        return system_prompt, api_messages

    @staticmethod
    # ast-grep-ignore: no-dict-any - Anthropic content format
    def _convert_user_content(msg: UserMessage) -> str | list[dict[str, Any]]:
        """Convert UserMessage content to Anthropic format."""
        if isinstance(msg.content, str):
            return msg.content

        # Multipart content
        # ast-grep-ignore: no-dict-any - Anthropic content block format
        blocks: list[dict[str, Any]] = []
        for part in msg.content:
            if isinstance(part, TextContentPart):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ImageUrlContentPart):
                url = part.image_url.get("url", "")
                if url.startswith("data:"):
                    # Parse data URI: data:<media_type>;base64,<data>
                    header, b64_data = url.split(",", 1)
                    media_type = header.split(":")[1].split(";")[0]
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        },
                    })
                else:
                    # URL-based image
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": url,
                        },
                    })
        return blocks

    @staticmethod
    def _merge_consecutive_roles(
        # ast-grep-ignore: no-dict-any - Anthropic API message dicts have heterogeneous value types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Anthropic API message dicts have heterogeneous value types
    ) -> list[dict[str, Any]]:
        """Merge consecutive messages with the same role.

        Anthropic requires alternating user/assistant roles. When multiple same-role
        messages appear consecutively (e.g., tool results followed by user message),
        merge their content blocks.
        """
        if not messages:
            return messages

        # ast-grep-ignore: no-dict-any - Anthropic message format
        merged: list[dict[str, Any]] = []
        for msg in messages:
            if merged and merged[-1]["role"] == msg["role"]:
                # Merge content into previous message
                prev_content = merged[-1]["content"]
                new_content = msg["content"]

                # Normalize both to lists
                if isinstance(prev_content, str):
                    prev_content = [{"type": "text", "text": prev_content}]
                if isinstance(new_content, str):
                    new_content = [{"type": "text", "text": new_content}]
                if not isinstance(prev_content, list):
                    prev_content = [prev_content]
                if not isinstance(new_content, list):
                    new_content = [new_content]

                merged[-1]["content"] = prev_content + new_content
            else:
                merged.append(msg.copy())

        return merged

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using Anthropic API."""
        self._validate_user_input(messages)

        start_time = time.monotonic()
        request_timestamp = datetime.now(UTC)
        request_id = f"anthropic_{uuid.uuid4().hex[:16]}"

        message_dicts = [message_to_json_dict(msg) for msg in messages]

        try:
            processed_messages = self._process_tool_messages(list(messages))

            system_prompt, api_messages = self._convert_messages_to_anthropic_format(
                processed_messages
            )

            # ast-grep-ignore: no-dict-any - Anthropic API params dict has heterogeneous value types
            params: dict[str, Any] = {
                "model": self.model,
                "messages": api_messages,
                "max_tokens": 8192,
                **self.default_kwargs,
                **self._get_model_specific_params(self.model),
            }

            if system_prompt:
                params["system"] = system_prompt

            if tools:
                params["tools"] = self._convert_tools_to_anthropic_format(tools)
                anthropic_tool_choice = self._convert_tool_choice_to_anthropic(
                    tool_choice
                )
                if anthropic_tool_choice:
                    params["tool_choice"] = anthropic_tool_choice

            response = await self.client.messages.create(**params)

            # Parse response
            content_text = ""
            tool_calls = []

            for block in response.content:
                if block.type == "text":
                    content_text += block.text
                elif block.type == "tool_use":
                    tool_calls.append(
                        ToolCallItem(
                            id=block.id,
                            type="function",
                            function=ToolCallFunction(
                                name=block.name,
                                arguments=json.dumps(block.input),
                            ),
                        )
                    )

            # Extract usage information
            reasoning_info = None
            if response.usage:
                reasoning_info = {
                    "prompt_tokens": response.usage.input_tokens,
                    "completion_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.input_tokens
                    + response.usage.output_tokens,
                }

            llm_output = LLMOutput(
                content=content_text or None,
                tool_calls=tool_calls if tool_calls else None,
                reasoning_info=reasoning_info,
            )

            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
                        messages=message_dicts,
                        tools=tools,
                        tool_choice=tool_choice,
                        response=asdict(llm_output),
                        duration_ms=duration_ms,
                        error=None,
                    )
                )
            except Exception as record_err:
                logger.debug(f"Failed to record LLM request: {record_err}")

            return llm_output

        except Exception as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
                        messages=message_dicts,
                        tools=tools,
                        tool_choice=tool_choice,
                        response=None,
                        duration_ms=duration_ms,
                        error=str(e),
                    )
                )
            except Exception as record_err:
                logger.debug(f"Failed to record LLM request error: {record_err}")

            self._raise_mapped_error(e)

    def _raise_mapped_error(self, e: Exception) -> NoReturn:
        """Map Anthropic SDK exceptions to our exception hierarchy."""
        if isinstance(e, anthropic.AuthenticationError):
            raise AuthenticationError(
                str(e), provider="anthropic", model=self.model
            ) from e
        elif isinstance(e, anthropic.RateLimitError):
            raise RateLimitError(str(e), provider="anthropic", model=self.model) from e
        elif isinstance(e, anthropic.NotFoundError):
            raise ModelNotFoundError(
                str(e), provider="anthropic", model=self.model
            ) from e
        elif isinstance(e, anthropic.BadRequestError):
            error_message = str(e)
            if (
                "context length" in error_message.lower()
                or "too many tokens" in error_message.lower()
            ):
                raise ContextLengthError(
                    error_message, provider="anthropic", model=self.model
                ) from e
            raise InvalidRequestError(
                error_message, provider="anthropic", model=self.model
            ) from e
        elif isinstance(e, anthropic.APIConnectionError):
            raise ProviderConnectionError(
                str(e), provider="anthropic", model=self.model
            ) from e
        elif isinstance(e, anthropic.APITimeoutError):
            raise ProviderTimeoutError(
                str(e), provider="anthropic", model=self.model
            ) from e
        elif isinstance(e, anthropic.InternalServerError):
            raise ServiceUnavailableError(
                str(e), provider="anthropic", model=self.model
            ) from e
        else:
            logger.error(f"Anthropic API error: {e}", exc_info=True)
            raise LLMProviderError(
                str(e), provider="anthropic", model=self.model
            ) from e

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Anthropic API message format has heterogeneous value types
    ) -> dict[str, object]:
        """Format user message with optional file content.

        Anthropic supports images via base64, PDFs via document blocks,
        and text files as inline content.
        """
        if not file_path:
            return {"role": "user", "content": prompt_text or ""}

        if mime_type and mime_type.startswith("image/"):
            async with aiofiles.open(file_path, "rb") as f:
                image_data = await f.read()

            base64_image = base64.b64encode(image_data).decode("utf-8")

            # ast-grep-ignore: no-dict-any - Anthropic image content blocks have heterogeneous value types
            content: list[dict[str, Any]] = []
            if prompt_text:
                content.append({"type": "text", "text": prompt_text})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": base64_image,
                },
            })

            return {"role": "user", "content": content}

        elif mime_type and mime_type == "application/pdf":
            # Anthropic supports PDFs natively via document blocks
            async with aiofiles.open(file_path, "rb") as f:
                pdf_data = await f.read()

            base64_pdf = base64.b64encode(pdf_data).decode("utf-8")

            # ast-grep-ignore: no-dict-any - Anthropic document content blocks have heterogeneous value types
            content = []
            if prompt_text:
                content.append({"type": "text", "text": prompt_text})
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64_pdf,
                },
            })

            return {"role": "user", "content": content}

        else:
            # Try reading as text, fall back to binary description on decode error
            try:
                async with aiofiles.open(file_path, encoding="utf-8") as f:
                    file_content = await f.read()
            except (UnicodeDecodeError, ValueError):
                description = (
                    f"[Binary file: {file_path} ({mime_type or 'unknown type'})"
                    f" - content cannot be displayed as text]"
                )
                if prompt_text:
                    return {
                        "role": "user",
                        "content": f"{prompt_text}\n\n{description}",
                    }
                return {"role": "user", "content": description}

            if max_text_length and len(file_content) > max_text_length:
                file_content = file_content[:max_text_length]
                logger.info(f"Truncated file content to {max_text_length} characters")

            if prompt_text:
                combined_content = f"{prompt_text}\n\nFile content:\n{file_content}"
            else:
                combined_content = file_content

            return {"role": "user", "content": combined_content}

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using Anthropic API."""
        self._validate_user_input(messages)
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses."""
        start_time = time.monotonic()
        request_timestamp = datetime.now(UTC)
        request_id = f"anthropic_stream_{uuid.uuid4().hex[:16]}"

        message_dicts = [message_to_json_dict(msg) for msg in messages]

        try:
            processed_messages = self._process_tool_messages(list(messages))

            system_prompt, api_messages = self._convert_messages_to_anthropic_format(
                processed_messages
            )

            # ast-grep-ignore: no-dict-any - Anthropic API params dict has heterogeneous value types
            params: dict[str, Any] = {
                "model": self.model,
                "messages": api_messages,
                "max_tokens": 8192,
                **self.default_kwargs,
                **self._get_model_specific_params(self.model),
            }

            if system_prompt:
                params["system"] = system_prompt

            if tools:
                params["tools"] = self._convert_tools_to_anthropic_format(tools)
                anthropic_tool_choice = self._convert_tool_choice_to_anthropic(
                    tool_choice
                )
                if anthropic_tool_choice:
                    params["tool_choice"] = anthropic_tool_choice

            # Check for VCR replay mode
            vcr_events = await self._maybe_parse_vcr_stream(params)
            if vcr_events is not None:
                for event in vcr_events:
                    yield event
                return

            # Use Anthropic streaming
            async with self.client.messages.stream(**params) as stream:
                # Track tool_use blocks being built
                # ast-grep-ignore: no-dict-any - Streaming accumulator
                current_tool: dict[str, Any] | None = None

                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            current_tool = {
                                "id": block.id,
                                "name": block.name,
                                "arguments": "",
                            }

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield LLMStreamEvent(type="content", content=delta.text)
                        elif (
                            delta.type == "input_json_delta"
                            and current_tool is not None
                        ):
                            current_tool["arguments"] += delta.partial_json

                    elif event.type == "content_block_stop":
                        if current_tool is not None:
                            tool_call = ToolCallItem(
                                id=current_tool["id"],
                                type="function",
                                function=ToolCallFunction(
                                    name=current_tool["name"],
                                    arguments=current_tool["arguments"] or "{}",
                                ),
                            )
                            yield LLMStreamEvent(
                                type="tool_call",
                                tool_call=tool_call,
                                tool_call_id=current_tool["id"],
                            )
                            current_tool = None

                # Get final message for usage info
                final_message = await stream.get_final_message()

            metadata: StreamingMetadata = {}
            if final_message and final_message.usage:
                metadata["reasoning_info"] = {
                    "prompt_tokens": final_message.usage.input_tokens,
                    "completion_tokens": final_message.usage.output_tokens,
                    "total_tokens": final_message.usage.input_tokens
                    + final_message.usage.output_tokens,
                }

            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
                        messages=message_dicts,
                        tools=tools,
                        tool_choice=tool_choice,
                        response={"streaming": True, "metadata": metadata},
                        duration_ms=duration_ms,
                        error=None,
                    )
                )
            except Exception as record_err:
                logger.debug(f"Failed to record streaming LLM request: {record_err}")

            yield LLMStreamEvent(type="done", metadata=metadata)

        except Exception as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
                        messages=message_dicts,
                        tools=tools,
                        tool_choice=tool_choice,
                        response=None,
                        duration_ms=duration_ms,
                        error=str(e),
                    )
                )
            except Exception as record_err:
                logger.debug(
                    f"Failed to record streaming LLM request error: {record_err}"
                )

            error_message = str(e)
            error_type = "unknown"
            if isinstance(e, anthropic.AuthenticationError):
                error_type = "authentication"
            elif isinstance(e, anthropic.RateLimitError):
                error_type = "rate_limit"
            elif isinstance(e, anthropic.NotFoundError):
                error_type = "model_not_found"
            elif isinstance(e, anthropic.BadRequestError):
                error_type = "invalid_request"
            elif isinstance(e, anthropic.APIConnectionError):
                error_type = "connection"
            elif isinstance(e, anthropic.APITimeoutError):
                error_type = "timeout"

            logger.error(
                f"Anthropic streaming API error ({error_type}): {e}", exc_info=True
            )
            yield LLMStreamEvent(
                type="error",
                error=error_message,
                metadata={
                    "error_id": str(e.__class__.__name__),
                    "error_type": error_type,
                    "provider": "anthropic",
                    "model": self.model,
                },
            )

    async def _maybe_parse_vcr_stream(
        self,
        # ast-grep-ignore: no-dict-any - Anthropic API params
        params: dict[str, Any],
    ) -> list[LLMStreamEvent] | None:
        """Parse VCR-recorded streaming responses for Anthropic.

        VCR records Anthropic SSE streaming responses. During replay, we need to
        make a non-streaming request and convert it to stream events, since VCR
        cannot properly replay SSE streams.
        """
        if (
            not os.getenv("PYTEST_CURRENT_TEST")
            and os.getenv("LLM_RECORD_MODE") != "replay"
        ):
            return None

        # In VCR replay mode, make a non-streaming request and convert to events.
        # Only catch connection/transport errors that indicate VCR isn't replaying.
        try:
            response = await self.client.messages.create(**params)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError):
            return None

        events: list[LLMStreamEvent] = []

        for block in response.content:
            if block.type == "text":
                events.append(LLMStreamEvent(type="content", content=block.text))
            elif block.type == "tool_use":
                tool_call = ToolCallItem(
                    id=block.id,
                    type="function",
                    function=ToolCallFunction(
                        name=block.name,
                        arguments=json.dumps(block.input),
                    ),
                )
                events.append(
                    LLMStreamEvent(
                        type="tool_call",
                        tool_call=tool_call,
                        tool_call_id=block.id,
                    )
                )

        metadata: StreamingMetadata = {}
        if response.usage:
            metadata["reasoning_info"] = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens
                + response.usage.output_tokens,
            }

        events.append(LLMStreamEvent(type="done", metadata=metadata))
        return events
