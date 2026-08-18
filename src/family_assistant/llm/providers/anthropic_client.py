"""
Direct Anthropic API implementation for LLM interactions.
"""

import base64
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NoReturn,
    TypedDict,
    TypeVar,
    cast,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

import aiofiles
import anthropic
from anthropic.types import (
    Base64ImageSourceParam,
    Base64PDFSourceParam,
    DocumentBlockParam,
    ImageBlockParam,
    TextBlockParam,
    ToolChoiceParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
    URLImageSourceParam,
)
from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from family_assistant.llm import (
    BaseLLMClient,
    JsonObject,
    LLMOutput,
    LLMStreamEvent,
    StreamEventMetadata,
    StructuredOutputError,
    ToolCallFunction,
    ToolCallItem,
    UserMessageDict,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPart,
    ImageUrlContentPart,
    LLMMessage,
    MessageReasoningInfo,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.utils.call_telemetry import LLMCallTelemetry
from family_assistant.tools.types import ToolDefinition

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

ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
_VALID_IMAGE_MEDIA_TYPES: set[str] = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}


logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")


class _StreamingToolAccumulator(TypedDict):
    id: str
    name: str
    arguments: str


# Thinking blocks carry a `signature` the API verifies on replay, so they are
# stored and replayed as opaque dicts rather than being re-derived from their
# parts. `redacted_thinking` blocks carry an opaque `data` payload instead.
_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


class AnthropicProviderMetadata(TypedDict):
    """Assistant-turn metadata persisted for the Anthropic provider.

    ``thinking_blocks`` holds the turn's `thinking` / `redacted_thinking` blocks
    exactly as the API returned them. They must be replayed byte-for-byte: the
    API rejects a request whose thinking block has an altered `signature` with
    ``400 Invalid 'signature' in 'thinking' block``. Their *position* within the
    turn is not checked, so only content fidelity has to be preserved.
    """

    provider: Literal["anthropic"]
    thinking_blocks: list[JsonObject]


class AnthropicClient(BaseLLMClient):
    """Direct Anthropic API implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        model_parameters: dict[str, dict[str, object]] | None = None,
        **kwargs: Any,  # noqa: ANN401 # Accepts arbitrary Anthropic API parameters
    ) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.model_parameters = model_parameters or {}
        self.default_kwargs = kwargs
        logger.info(
            f"AnthropicClient initialized for model: {model}, "
            f"model-specific parameters: {list(model_parameters.keys()) if model_parameters else []}"
        )

    def _supports_multimodal_tools(self) -> bool:
        """Anthropic supports images and PDFs in tool results natively."""
        return True

    @staticmethod
    def _extract_thinking_blocks(content: Sequence[object]) -> list[JsonObject]:
        """Pull thinking blocks off a response's content as plain JSON dicts."""
        blocks: list[JsonObject] = []
        for block in content:
            block_type = (
                block.get("type")
                if isinstance(block, dict)
                else getattr(block, "type", None)
            )
            if block_type not in _THINKING_BLOCK_TYPES:
                continue
            model_dump = getattr(block, "model_dump", None)
            if callable(model_dump):
                blocks.append(cast("JsonObject", model_dump(mode="json")))
            elif isinstance(block, dict):
                blocks.append(cast("JsonObject", dict(block)))
        return blocks

    @staticmethod
    def _thinking_metadata(
        thinking_blocks: list[JsonObject],
    ) -> AnthropicProviderMetadata | None:
        """Wrap thinking blocks as provider metadata, or None when there are none."""
        if not thinking_blocks:
            return None
        return AnthropicProviderMetadata(
            provider="anthropic", thinking_blocks=thinking_blocks
        )

    @staticmethod
    def _thinking_blocks_from_metadata(provider_metadata: object) -> list[JsonObject]:
        """Recover thinking blocks from a stored assistant message.

        Metadata from other providers (Gemini thought signatures, OpenAI Responses
        output) is ignored rather than rejected, so a conversation that switches
        provider mid-thread degrades to a plain replay instead of erroring.
        """
        if not isinstance(provider_metadata, dict):
            return []
        if provider_metadata.get("provider") != "anthropic":
            return []
        blocks = provider_metadata.get("thinking_blocks")
        if not isinstance(blocks, list):
            raise TypeError(
                "Anthropic provider metadata thinking_blocks must be a list"
            )
        validated_blocks: list[JsonObject] = []
        for index, block in enumerate(blocks):
            if (
                not isinstance(block, dict)
                or block.get("type") not in _THINKING_BLOCK_TYPES
            ):
                raise TypeError(
                    "Anthropic provider metadata thinking_blocks entries must be "
                    f"thinking or redacted_thinking objects; invalid entry at index {index}"
                )
            validated_blocks.append(cast("JsonObject", block))
        return validated_blocks

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
                text_block = TextBlockParam(type="text", text=original_msg.content)
                content: list[TextBlockParam | ImageBlockParam | DocumentBlockParam] = [
                    text_block,
                ]
                injection_msgs: list[LLMMessage] = []
                for attachment in attachments:
                    if (
                        attachment.content
                        and attachment.mime_type in _VALID_IMAGE_MEDIA_TYPES
                    ):
                        b64_data = attachment.get_content_as_base64()
                        if b64_data:
                            image_media = cast("ImageMediaType", attachment.mime_type)
                            content.append(
                                ImageBlockParam(
                                    type="image",
                                    source=Base64ImageSourceParam(
                                        type="base64",
                                        media_type=image_media,
                                        data=b64_data,
                                    ),
                                )
                            )
                    elif (
                        attachment.content and attachment.mime_type == "application/pdf"
                    ):
                        b64_data = attachment.get_content_as_base64()
                        if b64_data:
                            content.append(
                                DocumentBlockParam(
                                    type="document",
                                    source=Base64PDFSourceParam(
                                        type="base64",
                                        media_type="application/pdf",
                                        data=b64_data,
                                    ),
                                )
                            )
                    elif attachment.content or attachment.file_path:
                        if attachment.content:
                            logger.warning(
                                f"Unsupported attachment type {attachment.mime_type} for Anthropic, falling back to text description"
                            )
                        else:
                            logger.warning(
                                f"File-path-only attachment {attachment.file_path} for Anthropic, falling back to text description"
                            )
                        text_block["text"] += "\n[File content in following message]"
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

    def _create_native_output_tool(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, object],
    ) -> ToolParam:
        """Create a synthetic Anthropic tool for native structured output."""
        return {
            "name": name,
            "description": description,
            "input_schema": input_schema,
        }

    def _extract_forced_tool_input(
        self,
        response: Any,  # noqa: ANN401 - Anthropic SDK content block union
        tool_name: str,
    ) -> dict[str, object]:
        """Extract the input object from a forced tool-use response."""
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                if isinstance(block.input, dict):
                    return cast("dict[str, object]", block.input)
                raise TypeError("Anthropic tool input was not a JSON object")

        raise ValueError(
            f"Anthropic response did not include expected tool '{tool_name}'"
        )

    async def _generate_with_native_output_tool(
        self,
        *,
        messages: Sequence[LLMMessage],
        tool_name: str,
        description: str,
        input_schema: dict[str, object],
        max_retries: int,
        parse_output: Callable[[dict[str, object]], R],
        error_message_prefix: str,
    ) -> R:
        """Run a native Anthropic tool-use round-trip for structured output."""
        self._validate_user_input(messages)

        attempt_messages = list(messages)
        raw_response: str | None = None
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                processed_messages = self._process_tool_messages(attempt_messages)
                system_blocks, api_messages = (
                    self._convert_messages_to_anthropic_format(processed_messages)
                )
                # ast-grep-ignore: no-dict-any - kwargs dict for client.messages.create(**params) requires heterogeneous values
                params: dict[str, Any] = {
                    "model": self.model,
                    "messages": api_messages,
                    "max_tokens": 8192,
                    "tools": [
                        self._create_native_output_tool(
                            name=tool_name,
                            description=description,
                            input_schema=input_schema,
                        )
                    ],
                    "tool_choice": {"type": "tool", "name": tool_name},
                    **self.default_kwargs,
                    **self._get_model_specific_params(self.model),
                }
                # This path always forces the output tool, so thinking can never
                # be valid here regardless of how the model is configured.
                self._strip_thinking_for_forced_tool_choice(params)
                if system_blocks:
                    params["system"] = system_blocks

                response = await self.client.messages.create(**params)
                tool_input = self._extract_forced_tool_input(response, tool_name)
                raw_response = json.dumps(tool_input)
                return parse_output(tool_input)

            except (ValidationError, TypeError, ValueError) as e:
                last_error = e
                logger.warning(
                    f"{error_message_prefix} validation failed "
                    f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                if attempt < max_retries:
                    attempt_messages.append(
                        UserMessage(
                            content=(
                                f"Your previous tool input was invalid. Error: {e}\n\n"
                                f"Please call the '{tool_name}' tool again with a corrected JSON object."
                            )
                        )
                    )
            except Exception as e:
                self._raise_mapped_error(e)

        raise StructuredOutputError(
            message=(
                f"Failed to generate valid {error_message_prefix.lower()} after "
                f"{max_retries + 1} attempts"
            ),
            provider="anthropic",
            model=self.model,
            raw_response=raw_response,
            validation_error=last_error,
        )

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Generate structured output using Anthropic's native tool-use schema enforcement."""
        return await self._generate_with_native_output_tool(
            messages=messages,
            tool_name="return_structured_response",
            description="Return the final response as a structured JSON object.",
            input_schema=cast("dict[str, object]", response_model.model_json_schema()),
            max_retries=max_retries,
            parse_output=lambda tool_input: response_model.model_validate(tool_input),
            error_message_prefix="Anthropic structured output",
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Generate a JSON object using Anthropic's native tool-use mode."""
        return await self._generate_with_native_output_tool(
            messages=self._add_system_instruction(
                messages,
                (
                    "You must respond with a valid JSON object. "
                    "The required fields and structure are defined by the conversation. "
                    "Do not omit requested keys."
                ),
            ),
            tool_name="return_json_object",
            description="Return the final response as a JSON object.",
            input_schema={
                "type": "object",
                "additionalProperties": True,
            },
            max_retries=max_retries,
            parse_output=lambda tool_input: cast("JsonObject", tool_input),
            error_message_prefix="Anthropic JSON output",
        )

    @staticmethod
    def _convert_tools_to_anthropic_format(
        tools: list[ToolDefinition],
    ) -> list[ToolParam]:
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
    ) -> ToolChoiceParam | None:
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
        # ast-grep-ignore: no-dict-any - MessageParam TypedDict incompatible with dynamic content merging in _merge_consecutive_roles
    ) -> tuple[str | list[TextBlockParam] | None, list[dict[str, Any]]]:
        """Convert typed messages to Anthropic API format.

        Returns (system_blocks, api_messages).

        Key differences from OpenAI:
        - System messages are extracted to the top-level `system` parameter
        - Tool results use role: "user" with tool_result content blocks
        - Assistant tool calls use tool_use content blocks
        - Consecutive same-role messages are merged (Anthropic requires alternating roles)
        - Images use source.type: "base64" format
        """
        system_parts: list[str] = []
        # Stable-prefix length of the first system message, used to place the
        # prompt-cache breakpoint. Only the first is considered: it is the
        # profile's system prompt, and anything hoisted after it (mid-conversation
        # system triggers) is per-turn material that belongs past the breakpoint.
        stable_prefix_len: int | None = None
        # Index of the api_message carrying the first turn-scaffolding message.
        # It marks the boundary between replayed history and regenerated
        # material, which is where the conversation's cache breakpoint goes.
        first_scaffolding_index: int | None = None
        # ast-grep-ignore: no-dict-any - MessageParam TypedDict incompatible with dynamic content merging in _merge_consecutive_roles
        api_messages: list[dict[str, Any]] = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                if not system_parts:
                    stable_prefix_len = msg.stable_prefix_len
                system_parts.append(msg.content)

            elif isinstance(msg, UserMessage):
                content = self._convert_user_content(msg)
                if first_scaffolding_index is None and msg.is_turn_scaffolding:
                    first_scaffolding_index = len(api_messages)
                api_messages.append({"role": "user", "content": content})

            elif isinstance(msg, AssistantMessage):
                # Thinking leads the turn, mirroring the order the API itself
                # emits. Position is *not* enforced -- the API accepts thinking
                # after text or tool_use, and accepts the interleaved shape that
                # _merge_consecutive_roles can produce when two assistant turns
                # merge -- so no ordering machinery is warranted. What is
                # enforced is the signature, so blocks are replayed as opaque
                # dicts, byte-identical to what came back.
                content_blocks: list[
                    TextBlockParam | ToolUseBlockParam | JsonObject
                ] = list(self._thinking_blocks_from_metadata(msg.provider_metadata))
                if msg.content:
                    content_blocks.append(TextBlockParam(type="text", text=msg.content))
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        arguments = tc.function.arguments
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except json.JSONDecodeError as e:
                                raise InvalidRequestError(
                                    f"Malformed tool call arguments for "
                                    f"'{tc.function.name}' (id={tc.id}): {e}",
                                    provider="anthropic",
                                    model=self.model,
                                ) from e
                        content_blocks.append(
                            ToolUseBlockParam(
                                type="tool_use",
                                id=tc.id,
                                name=tc.function.name,
                                input=arguments,
                            )
                        )
                if content_blocks:
                    api_messages.append({
                        "role": "assistant",
                        "content": content_blocks,
                    })

            elif isinstance(msg, ToolMessage):
                # content may be a string or list of content blocks (from _process_tool_messages
                # for native multimodal support). Anthropic tool_result accepts both.
                tool_result_block = ToolResultBlockParam(
                    type="tool_result",
                    tool_use_id=msg.tool_call_id,
                    content=msg.content,
                )
                api_messages.append({"role": "user", "content": [tool_result_block]})

        # The history breakpoint goes on before merging, while each typed message
        # still maps to one api_message and the scaffolding index means something.
        self._mark_history_breakpoint(api_messages, first_scaffolding_index)

        # Merge consecutive same-role messages (Anthropic requires alternating roles)
        api_messages = self._merge_consecutive_roles(api_messages)

        # The trailing breakpoint goes on after, because merging is what turns the
        # turn's last user messages into the block list that can carry one.
        if api_messages:
            self._mark_cache_breakpoint(api_messages[-1])

        system_blocks = self._build_system_blocks(system_parts, stable_prefix_len)
        return system_blocks, api_messages

    @classmethod
    def _mark_history_breakpoint(
        cls,
        # ast-grep-ignore: no-dict-any - MessageParam TypedDict incompatible with the dynamic content this inspects
        api_messages: list[dict[str, Any]],
        first_scaffolding_index: int | None,
    ) -> None:
        """Mark the cache breakpoint that ends the replayable history.

        Anthropic caches only up to an explicit breakpoint, so the system-prompt
        breakpoint alone leaves the whole conversation re-read on every request.
        A conversation needs two more, because the two things worth caching end in
        different places. This is the first: everything ahead of the turn-context
        block is history the next turn replays byte-identically. The block itself
        is regenerated each turn and never persisted, so a breakpoint past it
        caches a prefix the next turn cannot match -- cache writes that are never
        read. (The second is placed at the very end, after merging, so a tool loop
        can read the results it has already accumulated.)

        It deliberately skips back over the whole run of user messages preceding
        the block rather than landing on the one just before it. Those messages
        merge with the block, and merging rewrites string content into a block
        list -- so the same historical message would go out as a bare string on
        the turn it is plain history and as a one-element list on the turn it sits
        next to the block, which is not the byte-identical prefix a cache read
        needs. Anchoring on the newest message that does *not* merge with the
        block keeps its serialization stable across turns. The cost is that the
        current turn's own user message falls outside the cached prefix, which is
        a message or two of text.

        Both breakpoints move as the conversation grows, which is the intended
        incremental pattern: each request writes only the delta past the previous
        one. Three in total including the system block, inside Anthropic's limit
        of four.
        """
        if first_scaffolding_index is None:
            return

        index = first_scaffolding_index - 1
        while index >= 0 and api_messages[index]["role"] == "user":
            index -= 1
        if index >= 0:
            cls._mark_cache_breakpoint(api_messages[index])

    @staticmethod
    def _mark_cache_breakpoint(
        # ast-grep-ignore: no-dict-any - MessageParam TypedDict incompatible with the dynamic content this inspects
        api_message: dict[str, Any],
    ) -> None:
        """Put a breakpoint at the end of *api_message*, if it can carry one.

        String content is left alone rather than wrapped into a one-element block
        list. Rewriting it would change the shape of a message on the wire, and a
        message that goes out as a bare string on one turn and a list on another
        is not the byte-identical prefix a cache read needs. Nothing is lost: in a
        real turn both breakpoints land on block lists anyway -- the trailing one
        on the user turn the context block merged into or on a tool result, and
        the other on an assistant turn.

        Thinking blocks are skipped: they are replayed as opaque dicts and their
        signatures are validated against exactly what came back, so adding a key
        to one risks rejecting the turn. They lead an assistant turn rather than
        ending it, but a merge of two assistant turns can interleave them, so the
        scan walks back to the newest block that is safe to annotate.
        """
        content = api_message["content"]
        if not isinstance(content, list):
            return

        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") in _THINKING_BLOCK_TYPES:
                continue
            # ast-grep-ignore: no-dict-any - block is a provider TypedDict at type level, a plain dict at runtime
            cast("dict[str, Any]", block)["cache_control"] = {"type": "ephemeral"}
            return

    @staticmethod
    def _reasoning_info_from_usage(
        usage: Any,  # noqa: ANN401 - anthropic.types.Usage, shape varies by SDK version
    ) -> MessageReasoningInfo:
        """Build reasoning info from an Anthropic usage object.

        Anthropic splits prompt tokens into three disjoint buckets: `input_tokens`
        counts only the uncached remainder, with cache reads and cache writes
        reported separately. `total_tokens` therefore has to add all three back
        together, or cached turns will under-report the prompt.
        """
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        cache_write = getattr(usage, "cache_creation_input_tokens", None)

        reasoning_info = MessageReasoningInfo(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=(
                usage.input_tokens
                + (cache_read or 0)
                + (cache_write or 0)
                + usage.output_tokens
            ),
        )
        # A reported zero is a known cache miss and is recorded as such. Only an
        # absent field is left out, so a miss cannot be mistaken for a provider
        # that reports nothing -- otherwise a dashboard that skips unreported
        # values would drop every miss and overstate the hit rate.
        if cache_read is not None:
            reasoning_info["cached_prompt_tokens"] = cache_read
        if cache_write is not None:
            reasoning_info["cache_write_tokens"] = cache_write
        return reasoning_info

    @staticmethod
    def _build_system_blocks(
        system_parts: list[str],
        stable_prefix_len: int | None,
    ) -> str | list[TextBlockParam] | None:
        """Build the top-level `system` value, with a prompt-cache breakpoint.

        Prompt caching is a prefix match, so a breakpoint only pays off when the
        text ahead of it is byte-identical between requests. The profile's system
        prompt ends with per-turn material (current time, aggregated context), so
        we split it at `stable_prefix_len` and mark only the leading block
        cacheable. Concatenating the returned blocks reproduces the single string
        this used to send, so the model sees identical text either way.

        A prompt that is stable all the way to the end needs no split -- it is
        emitted as one cacheable block. Only when there is no boundary at all is
        the plain string returned, since a breakpoint over volatile text buys
        cache writes and no reads.
        """
        if not system_parts:
            return None

        system_prompt = "\n\n".join(system_parts)
        if stable_prefix_len is None or stable_prefix_len <= 0:
            return system_prompt

        if stable_prefix_len >= len(system_prompt):
            return [
                TextBlockParam(
                    type="text",
                    text=system_prompt,
                    cache_control={"type": "ephemeral"},
                )
            ]

        return [
            TextBlockParam(
                type="text",
                text=system_prompt[:stable_prefix_len],
                cache_control={"type": "ephemeral"},
            ),
            TextBlockParam(type="text", text=system_prompt[stable_prefix_len:]),
        ]

    @staticmethod
    def _convert_user_content(
        msg: UserMessage,
    ) -> str | list[TextBlockParam | ImageBlockParam]:
        """Convert UserMessage content to Anthropic format."""
        if isinstance(msg.content, str):
            return msg.content

        # Multipart content
        blocks: list[TextBlockParam | ImageBlockParam] = []
        for part in msg.content:
            if isinstance(part, TextContentPart):
                blocks.append(TextBlockParam(type="text", text=part.text))
            elif isinstance(part, ImageUrlContentPart):
                url = part.image_url.get("url", "")
                if url.startswith("data:"):
                    # Parse data URI: data:<media_type>;base64,<data>
                    header, b64_data = url.split(",", 1)
                    raw_media_type = header.split(":")[1].split(";")[0]
                    if raw_media_type not in _VALID_IMAGE_MEDIA_TYPES:
                        logger.warning(
                            f"Unsupported image media type '{raw_media_type}' for Anthropic, skipping"
                        )
                        continue
                    blocks.append(
                        ImageBlockParam(
                            type="image",
                            source=Base64ImageSourceParam(
                                type="base64",
                                media_type=cast("ImageMediaType", raw_media_type),
                                data=b64_data,
                            ),
                        )
                    )
                else:
                    # URL-based image
                    blocks.append(
                        ImageBlockParam(
                            type="image",
                            source=URLImageSourceParam(
                                type="url",
                                url=url,
                            ),
                        )
                    )
        return blocks

    @staticmethod
    def _merge_consecutive_roles(
        # ast-grep-ignore: no-dict-any - MessageParam TypedDict incompatible with dynamic content normalization (str->list, list concatenation)
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - MessageParam TypedDict incompatible with dynamic content normalization (str->list, list concatenation)
    ) -> list[dict[str, Any]]:
        """Merge consecutive messages with the same role.

        Anthropic requires alternating user/assistant roles. When multiple same-role
        messages appear consecutively (e.g., tool results followed by user message),
        merge their content blocks.
        """
        if not messages:
            return messages

        # ast-grep-ignore: no-dict-any - MessageParam TypedDict incompatible with dynamic content normalization (str->list, list concatenation)
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

    def _build_request_params(
        self,
        # ast-grep-ignore: no-dict-any - MessageParam TypedDict incompatible with dynamic content merging in _merge_consecutive_roles
        api_messages: list[dict[str, Any]],
        system_blocks: str | list[TextBlockParam] | None,
        tools: list[ToolDefinition] | None,
        tool_choice: str | None,
        # ast-grep-ignore: no-dict-any - kwargs dict for client.messages.create(**params) requires heterogeneous values
    ) -> dict[str, Any]:
        """Assemble the kwargs for a messages.create / messages.stream call.

        Shared by the streaming and non-streaming paths so that thinking
        configuration is validated identically on both.
        """
        # ast-grep-ignore: no-dict-any - kwargs dict for client.messages.create(**params) requires heterogeneous values
        params: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": 8192,
            **self.default_kwargs,
            **self._get_model_specific_params(self.model),
        }

        self._validate_thinking_params(params)

        if system_blocks:
            params["system"] = system_blocks

        if tools:
            params["tools"] = self._convert_tools_to_anthropic_format(tools)
            anthropic_tool_choice = self._convert_tool_choice_to_anthropic(tool_choice)
            if anthropic_tool_choice:
                params["tool_choice"] = anthropic_tool_choice
                if self._forces_a_tool(anthropic_tool_choice):
                    self._strip_thinking_for_forced_tool_choice(params)

        return params

    @staticmethod
    def _strip_thinking_for_forced_tool_choice(
        # ast-grep-ignore: no-dict-any - kwargs dict for client.messages.create(**params) requires heterogeneous values
        params: dict[str, Any],
    ) -> None:
        """Drop thinking parameters from a request that forces a tool choice.

        Anthropic rejects thinking combined with a forced ``tool`` or ``any``
        choice. ``llm_parameters`` is keyed by model rather than by call path, so
        a model configured for thinking carries it into every request -- including
        structured output and ``generate_json``, which force a specific tool and
        therefore cannot use the reasoning anyway.

        Removed rather than set to a disabled value on purpose: an explicit
        ``thinking: {"type": "disabled"}`` is itself a 400 on some generations
        (claude-fable-5), so the only portable way to turn it off is to say
        nothing. Mutates in place, and is a no-op for a model without thinking
        configured.
        """
        for key in ("thinking", "output_config"):
            if params.pop(key, None) is not None:
                logger.debug(
                    "Dropped '%s' from a forced-tool-choice request; Anthropic "
                    "rejects thinking with a forced tool choice.",
                    key,
                )

    @staticmethod
    def _forces_a_tool(tool_choice: "ToolChoiceParam | None") -> bool:
        """Whether this choice compels the model to call a tool."""
        return tool_choice is not None and tool_choice.get("type") in {"any", "tool"}

    def _validate_thinking_params(
        self,
        # ast-grep-ignore: no-dict-any - kwargs dict for client.messages.create(**params) requires heterogeneous values
        params: dict[str, Any],
    ) -> None:
        """Fail fast on a thinking budget that cannot fit in ``max_tokens``.

        The API requires ``budget_tokens < max_tokens``. Catching it here turns
        a mid-conversation 400 into a startup-shaped configuration error naming
        both values.

        The two thinking shapes are not interchangeable across model
        generations -- ``{"type": "enabled", "budget_tokens": N}`` versus
        ``{"type": "adaptive"}`` with ``output_config.effort`` -- and only the
        former carries a budget, so only the former is checked here. An
        unsupported shape is left to the API, which names the model and the
        expected alternative better than a local guess could.
        """
        thinking = params.get("thinking")
        if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
            return

        budget = thinking.get("budget_tokens")
        max_tokens = params.get("max_tokens")
        if not isinstance(budget, int) or not isinstance(max_tokens, int):
            return

        if budget >= max_tokens:
            raise InvalidRequestError(
                f"Thinking budget_tokens ({budget}) must be less than max_tokens "
                f"({max_tokens}). Raise max_tokens for model '{self.model}' in "
                f"llm_parameters, or lower the thinking budget.",
                provider="anthropic",
                model=self.model,
            )

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using Anthropic API."""
        self._validate_user_input(messages)

        with tracer.start_as_current_span("llm.provider.generate") as span:
            telemetry = LLMCallTelemetry(
                span,
                provider="anthropic",
                system="anthropic",
                requested_model=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                streaming=False,
            )

            try:
                processed_messages = self._process_tool_messages(list(messages))

                system_blocks, api_messages = (
                    self._convert_messages_to_anthropic_format(processed_messages)
                )

                params = self._build_request_params(
                    api_messages, system_blocks, tools, tool_choice
                )

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

                thinking_blocks = self._extract_thinking_blocks(response.content)

                telemetry.record_response_metadata(
                    resolved_model=response.model,
                    response_id=response.id,
                    finish_reason=response.stop_reason,
                )

                reasoning_info: MessageReasoningInfo | None = None
                if response.usage:
                    reasoning_info = self._reasoning_info_from_usage(response.usage)

                llm_output = LLMOutput(
                    content=content_text or None,
                    tool_calls=tool_calls if tool_calls else None,
                    reasoning_info=reasoning_info,
                    provider_metadata=self._thinking_metadata(thinking_blocks),
                    resolved_model=response.model,
                )

                telemetry.record_output(llm_output)
                telemetry.finish_success(asdict(llm_output))

                return llm_output

            except Exception as e:
                telemetry.finish_error(e)
                self._raise_mapped_error(e)

    def _raise_mapped_error(self, e: Exception) -> NoReturn:
        """Map Anthropic SDK exceptions to our exception hierarchy."""
        if isinstance(e, LLMProviderError):
            raise e
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
            logger.error(f"Anthropic API error: {e}", exc_info=e)
            raise LLMProviderError(
                str(e), provider="anthropic", model=self.model
            ) from e

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        """Format user message with optional file content.

        Anthropic supports images via base64, PDFs via document blocks,
        and text files as inline content.
        """
        if not file_path:
            return {"role": "user", "content": prompt_text or ""}

        if mime_type and mime_type in _VALID_IMAGE_MEDIA_TYPES:
            async with aiofiles.open(file_path, "rb") as f:
                image_data = await f.read()

            base64_image = base64.b64encode(image_data).decode("utf-8")

            content: list[TextBlockParam | ImageBlockParam] = []
            if prompt_text:
                content.append(TextBlockParam(type="text", text=prompt_text))
            content.append(
                ImageBlockParam(
                    type="image",
                    source=Base64ImageSourceParam(
                        type="base64",
                        media_type=cast("ImageMediaType", mime_type),
                        data=base64_image,
                    ),
                )
            )

            return cast("UserMessageDict", {"role": "user", "content": content})

        elif mime_type and mime_type == "application/pdf":
            # Anthropic supports PDFs natively via document blocks
            async with aiofiles.open(file_path, "rb") as f:
                pdf_data = await f.read()

            base64_pdf = base64.b64encode(pdf_data).decode("utf-8")

            pdf_content: list[TextBlockParam | DocumentBlockParam] = []
            if prompt_text:
                pdf_content.append(TextBlockParam(type="text", text=prompt_text))
            pdf_content.append(
                DocumentBlockParam(
                    type="document",
                    source=Base64PDFSourceParam(
                        type="base64",
                        media_type="application/pdf",
                        data=base64_pdf,
                    ),
                )
            )

            return cast("UserMessageDict", {"role": "user", "content": pdf_content})

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
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using Anthropic API."""
        self._validate_user_input(messages)
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses."""
        span = tracer.start_span("llm.provider.generate_stream")
        try:
            telemetry = LLMCallTelemetry(
                span,
                provider="anthropic",
                system="anthropic",
                requested_model=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                streaming=True,
            )

            try:
                processed_messages = self._process_tool_messages(list(messages))

                system_blocks, api_messages = (
                    self._convert_messages_to_anthropic_format(processed_messages)
                )

                params = self._build_request_params(
                    api_messages, system_blocks, tools, tool_choice
                )

                # Check for VCR replay mode
                vcr_events = await self._maybe_parse_vcr_stream(params)
                if vcr_events is not None:
                    for event in vcr_events:
                        yield event
                    return

                # Use Anthropic streaming
                with trace.use_span(span, end_on_exit=False):
                    async with self.client.messages.stream(**params) as stream:
                        current_tool: _StreamingToolAccumulator | None = None

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
                                    content_event = LLMStreamEvent(
                                        type="content", content=delta.text
                                    )
                                    telemetry.observe_event(content_event)
                                    yield content_event  # noqa: ASYNC119
                                elif delta.type == "thinking_delta":
                                    # Surfaced as its own event type so it can
                                    # never be mistaken for response content.
                                    # The authoritative copy used for replay is
                                    # taken from the final message below, which
                                    # carries the signature these deltas lack.
                                    thinking_event = LLMStreamEvent(
                                        type="thinking", content=delta.thinking
                                    )
                                    telemetry.observe_event(thinking_event)
                                    yield thinking_event  # noqa: ASYNC119
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
                                    tool_call_event = LLMStreamEvent(
                                        type="tool_call",
                                        tool_call=tool_call,
                                        tool_call_id=current_tool["id"],
                                    )
                                    telemetry.observe_event(tool_call_event)
                                    yield tool_call_event  # noqa: ASYNC119
                                    current_tool = None

                        # Get final message for usage info
                        final_message = await stream.get_final_message()

                metadata: StreamEventMetadata = {}
                if final_message:
                    # Taken from the assembled final message rather than the
                    # deltas: only this copy carries the `signature` the API
                    # verifies when the block is replayed.
                    thinking_metadata = self._thinking_metadata(
                        self._extract_thinking_blocks(final_message.content)
                    )
                    if thinking_metadata:
                        metadata["provider_metadata"] = thinking_metadata
                if final_message and final_message.usage:
                    stream_reasoning_info = self._reasoning_info_from_usage(
                        final_message.usage
                    )
                    metadata["reasoning_info"] = stream_reasoning_info
                    telemetry.record_usage(stream_reasoning_info)

                if final_message:
                    telemetry.record_response_metadata(
                        resolved_model=final_message.model,
                        response_id=final_message.id,
                        finish_reason=final_message.stop_reason,
                    )
                    metadata["resolved_model"] = final_message.model

                telemetry.finish_success({"streaming": True, "metadata": metadata})

                yield LLMStreamEvent(type="done", metadata=metadata)

            except Exception as e:
                telemetry.finish_error(e)

                error_message = str(e)
                error_type = "unknown"
                if isinstance(e, InvalidRequestError):
                    error_type = "invalid_request"
                elif isinstance(e, anthropic.AuthenticationError):
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

                logger.exception(f"Anthropic streaming API error ({error_type}): {e}")
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
        finally:
            span.end()

    async def _maybe_parse_vcr_stream(
        self,
        # ast-grep-ignore: no-dict-any - kwargs dict for client.messages.create(**params) requires heterogeneous values
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
            elif block.type == "thinking":
                # One event per block rather than per delta -- a non-streaming
                # response has no deltas to replay. Emitting it at all is what
                # keeps this path's event sequence comparable to the real one.
                events.append(LLMStreamEvent(type="thinking", content=block.thinking))
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

        metadata: StreamEventMetadata = {}
        if response.usage:
            metadata["reasoning_info"] = self._reasoning_info_from_usage(response.usage)
        # Thinking blocks must reach the done event here exactly as they do on
        # the live streaming path. Without this, every test exercising Anthropic
        # streaming would silently run without reasoning state and prove nothing
        # about the round trip.
        thinking_metadata = self._thinking_metadata(
            self._extract_thinking_blocks(response.content)
        )
        if thinking_metadata:
            metadata["provider_metadata"] = thinking_metadata

        events.append(LLMStreamEvent(type="done", metadata=metadata))
        return events
