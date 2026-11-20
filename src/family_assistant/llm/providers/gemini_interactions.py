"""Gemini chat on the Interactions API: history as steps, and responses back.

Chat runs statelessly (``store=False``): every request carries the whole
conversation as Interactions steps. The model's steps must come back as the
API sent them, signatures included -- a tool call without one is rejected.
Its text and tool calls are already the assistant message's own ``content``
and ``tool_calls``, so ``provider_metadata`` keeps only what the message cannot
say: the opaque steps (thoughts and their signatures, tool searches,
server-side tool runs) in order, with an anchor where each piece of text or
tool call went. Replay fills the anchors from the message, so the message
stays the one record of what the model said. A message with nothing stored
(from ``generateContent``, another provider, or a compacted turn) is the case
with no opaque steps to interleave.

Everything here is pure data shaping; the client in ``google_genai_client``
owns the requests. See docs/design/gemini-interactions-chat.md.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from google.genai import types

from family_assistant.llm.messages import AssistantMessage

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from family_assistant.llm import JsonObject, JsonValue
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition

logger = logging.getLogger(__name__)

INTERACTION_REPLAY_KEY = "replay_steps"

# The documented placeholder for a tool call whose signature was never
# received. It lets history from generateContent, another provider, or a
# compacted turn be replayed; Interactions rejects an unsigned tool call.
SKIP_SIGNATURE = base64.b64encode(b"skip_thought_signature_validator").decode()

_DOCUMENT_MIME_TYPES = frozenset({
    "application/pdf",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.oasis.opendocument.spreadsheet",
})

_RESOLVED_MEDIA_KINDS = frozenset({"image", "video", "document"})

_CONTENT_DELTA_KINDS = frozenset({"text", "image", "audio", "document", "video"})

# GenerateContentConfig fields with an equivalent of the same name in the
# Interactions generation config.
_SAME_NAMED_GENERATION_FIELDS = (
    "temperature",
    "top_p",
    "seed",
    "max_output_tokens",
    "stop_sequences",
)


class UnrepresentableInteractionInputError(ValueError):
    """A history part that the Interactions API has no form for."""


# The provider tag of recorded steps. Not "google": message history reads
# that tag back as GeminiProviderMetadata (an agent's interaction id, a
# generateContent signature), which has no place for steps and drops them.
INTERACTION_STEPS_PROVIDER = "gemini_interactions"


def _anchor(step: JsonObject) -> JsonObject:
    """*step* without what the assistant message already records.

    A tool call keeps its id (and anything else, such as a signature) but not
    its name and arguments; a text-only model output keeps nothing but its
    place. Every other step is opaque and kept whole.
    """
    kind = step.get("type")
    if kind == "function_call" and isinstance(step.get("id"), str):
        return {k: v for k, v in step.items() if k not in {"name", "arguments"}}
    if kind == "model_output" and all(
        block.get("type") == "text" for block in _blocks(step.get("content"))
    ):
        return {k: v for k, v in step.items() if k != "content"}
    return step


def interaction_replay_metadata(steps: Sequence[JsonObject]) -> JsonObject | None:
    """``provider_metadata`` keeping what replaying a response needs.

    ``None`` when the response had nothing but text and tool calls: the
    message alone then renders it.
    """
    stored = [_anchor(step) for step in steps]
    if all(_is_anchor(step) for step in stored):
        return None
    return {
        "provider": INTERACTION_STEPS_PROVIDER,
        INTERACTION_REPLAY_KEY: cast("JsonValue", stored),
    }


def _is_anchor(step: JsonObject) -> bool:
    kind = step.get("type")
    return (kind == "function_call" and "name" not in step) or (
        kind == "model_output" and "content" not in step
    )


def stored_replay_steps(message: LLMMessage) -> list[JsonObject] | None:
    """The replay steps stored on *message*, if it came from Interactions chat."""
    if not isinstance(message, AssistantMessage):
        return None
    metadata = message.provider_metadata
    if (
        not isinstance(metadata, dict)
        or metadata.get("provider") != INTERACTION_STEPS_PROVIDER
    ):
        return None
    steps = metadata.get(INTERACTION_REPLAY_KEY)
    if not isinstance(steps, list) or not steps:
        return None
    return cast("list[JsonObject]", steps)


def _call_arguments(arguments: str | dict[str, object]) -> JsonValue:
    if isinstance(arguments, dict):
        return cast("JsonValue", arguments)
    parsed = json.loads(arguments) if arguments else {}
    return cast("JsonValue", parsed if isinstance(parsed, dict) else {})


def _replayed(
    message: AssistantMessage, stored: Sequence[JsonObject]
) -> list[JsonObject]:
    """*stored* with its anchors filled from *message*.

    The message is authoritative: a tool call it no longer has is left out,
    and text or a call it has that no anchor places (a message changed after
    the response) is added after the stored steps, unsigned.
    """
    calls = {call.id: call for call in message.tool_calls or ()}
    placed: set[str] = set()
    text_placed = False
    steps: list[JsonObject] = []
    for step in stored:
        kind = step.get("type")
        if kind == "function_call" and "name" not in step:
            call = calls.get(str(step.get("id")))
            if call is None:
                continue
            placed.add(call.id)
            steps.append({
                **step,
                "name": call.function.name,
                "arguments": _call_arguments(call.function.arguments),
            })
        elif kind == "model_output" and "content" not in step:
            if message.content and not text_placed:
                text_placed = True
                steps.append({
                    **step,
                    "content": [{"type": "text", "text": message.content}],
                })
        else:
            steps.append(step)
    if message.content and not text_placed:
        steps.append({
            "type": "model_output",
            "content": [{"type": "text", "text": message.content}],
        })
    steps.extend(
        {
            "type": "function_call",
            "id": call.id,
            "name": call.function.name,
            "arguments": _call_arguments(call.function.arguments),
            "signature": SKIP_SIGNATURE,
        }
        for call in message.tool_calls or ()
        if call.id not in placed
    )
    return steps


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _media_kind(mime_type: str) -> str:
    for prefix in ("image", "audio", "video"):
        if mime_type.startswith(f"{prefix}/"):
            return prefix
    return "document"


def _is_text_mime_type(mime_type: str) -> bool:
    return mime_type not in _DOCUMENT_MIME_TYPES and (
        mime_type.startswith("text/") or mime_type == "application/json"
    )


def _media_block(
    mime_type: str,
    *,
    media_resolution: str | None,
    data: bytes | None = None,
    uri: str | None = None,
) -> JsonObject:
    if data is not None and _is_text_mime_type(mime_type):
        return {"type": "text", "text": data.decode("utf-8", errors="replace")}
    kind = _media_kind(mime_type)
    block: JsonObject = {"type": kind, "mime_type": mime_type}
    if data is not None:
        block["data"] = _b64(data)
    if uri is not None:
        block["uri"] = uri
    if media_resolution is not None and kind in _RESOLVED_MEDIA_KINDS:
        block["resolution"] = media_resolution
    return block


def _part_content(part: types.Part, *, media_resolution: str | None) -> JsonObject:
    if part.text is not None:
        return {"type": "text", "text": part.text}
    if part.inline_data is not None and part.inline_data.data is not None:
        return _media_block(
            part.inline_data.mime_type or "application/octet-stream",
            media_resolution=media_resolution,
            data=part.inline_data.data,
        )
    if part.file_data is not None and part.file_data.file_uri is not None:
        return _media_block(
            part.file_data.mime_type or "application/octet-stream",
            media_resolution=media_resolution,
            uri=part.file_data.file_uri,
        )
    present = sorted(part.model_dump(exclude_none=True))
    raise UnrepresentableInteractionInputError(
        f"Gemini Interactions has no form for a history part carrying {present}"
    )


def _function_call_step(part: types.Part) -> JsonObject:
    call = part.function_call
    assert call is not None
    if not call.id or not call.name:
        raise UnrepresentableInteractionInputError(
            "A tool call in history has no id or name to replay it with"
        )
    signature = part.thought_signature
    return {
        "type": "function_call",
        "id": call.id,
        "name": call.name,
        "arguments": cast("JsonValue", call.args or {}),
        "signature": _b64(signature) if signature else SKIP_SIGNATURE,
    }


def _function_result_step(
    part: types.Part, *, media_resolution: str | None
) -> JsonObject:
    response = part.function_response
    assert response is not None
    if not response.id:
        raise UnrepresentableInteractionInputError(
            f"The result of tool {response.name!r} in history has no call id"
        )
    payload = response.response or {}
    text = (
        payload["result"]
        if set(payload) == {"result"} and isinstance(payload["result"], str)
        else json.dumps(payload, default=str)
    )
    result: list[JsonValue] = [{"type": "text", "text": text}]
    for media in response.parts or []:
        blob = media.inline_data
        if blob is not None and blob.data is not None:
            result.append(
                _media_block(
                    blob.mime_type or "application/octet-stream",
                    media_resolution=media_resolution,
                    data=blob.data,
                )
            )
    step: JsonObject = {
        "type": "function_result",
        "call_id": response.id,
        "result": result,
    }
    if response.name:
        step["name"] = response.name
    return step


def contents_to_steps(
    contents: Sequence[types.Content], *, media_resolution: str | None
) -> list[JsonObject]:
    """Interactions steps for ``generateContent``-shaped contents.

    The chat client already turns messages into contents -- attachments,
    images, multimodal tool results and all -- so history is rendered by
    translating that output rather than by converting each message twice.
    """
    steps: list[JsonObject] = []
    for content in contents:
        parts = content.parts or []
        if content.role == "model":
            texts: list[JsonValue] = []
            for part in parts:
                if part.function_call is not None:
                    if texts:
                        steps.append({"type": "model_output", "content": texts})
                        texts = []
                    steps.append(_function_call_step(part))
                elif part.text is not None and not part.thought:
                    texts.append({"type": "text", "text": part.text})
                elif not part.thought:
                    raise UnrepresentableInteractionInputError(
                        "Gemini Interactions has no form for a model part carrying "
                        f"{sorted(part.model_dump(exclude_none=True))}"
                    )
            if texts:
                steps.append({"type": "model_output", "content": texts})
            continue
        blocks: list[JsonValue] = []
        for part in parts:
            if part.function_response is not None:
                if blocks:
                    steps.append({"type": "user_input", "content": blocks})
                    blocks = []
                steps.append(
                    _function_result_step(part, media_resolution=media_resolution)
                )
            else:
                blocks.append(_part_content(part, media_resolution=media_resolution))
        if blocks:
            steps.append({"type": "user_input", "content": blocks})
    return steps


_JSON_SCHEMA_TYPES = frozenset({
    "string",
    "number",
    "integer",
    "boolean",
    "object",
    "array",
    "null",
})

# Keywords whose values are data, not schemas, and so are never rewritten.
_SCHEMA_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})


def _interaction_schema(schema: object, fixes: list[str]) -> object:
    """*schema* in the JSON Schema dialect Interactions accepts.

    Interactions takes JSON Schema and ignores the keywords it does not
    enforce, with two exceptions that reject the whole request with an opaque
    "Invalid JSON payload": a ``type`` that is not a JSON Schema type, and
    draft-07 tuple-form ``items``. MCP servers send both, so the first is
    dropped (the value is then unconstrained) and the second is rewritten as
    the equivalent ``prefixItems``.
    """
    if isinstance(schema, list):
        return [_interaction_schema(item, fixes) for item in schema]
    if not isinstance(schema, dict):
        return schema
    normalized: dict[str, object] = {}
    for key, value in schema.items():
        if key in _SCHEMA_DATA_KEYWORDS:
            normalized[key] = value
        elif key == "type" and isinstance(value, (str, list)):
            names = [value] if isinstance(value, str) else value
            valid = [name for name in names if name in _JSON_SCHEMA_TYPES]
            if len(valid) != len(names):
                fixes.append(f"type {value!r}")
            if valid:
                normalized[key] = valid[0] if isinstance(value, str) else valid
        elif key == "items" and isinstance(value, list):
            fixes.append("tuple-form items")
            normalized.setdefault("prefixItems", _interaction_schema(value, fixes))
        else:
            normalized[key] = _interaction_schema(value, fixes)
    return normalized


def function_tools(tools: Sequence[ToolDefinition]) -> list[JsonObject]:
    """Interactions function tools for OpenAI-style definitions.

    The JSON Schema goes through nearly as written (see
    ``_interaction_schema``), so nested objects and enums reach the model
    instead of being flattened the way the generateContent converter must.
    """
    converted: list[JsonObject] = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        function = tool["function"]
        fixes: list[str] = []
        parameters = _interaction_schema(
            function.get("parameters") or {"type": "object", "properties": {}}, fixes
        )
        if fixes:
            logger.warning(
                "Tool %r: rewrote %s for the Gemini Interactions schema dialect",
                function["name"],
                ", ".join(sorted(set(fixes))),
            )
        converted.append({
            "type": "function",
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": cast("JsonValue", parameters),
        })
    return converted


def tool_choice_setting(tool_choice: str | None) -> JsonValue | None:
    """The Interactions ``tool_choice`` for one of our tool choices."""
    if tool_choice in {None, "auto"}:
        return None
    if tool_choice == "none":
        return "none"
    if tool_choice == "required":
        return "any"
    return {"allowed_tools": {"mode": "any", "tools": [tool_choice]}}


@dataclass(frozen=True)
class InteractionGenerationSettings:
    """Configured request parameters, in the Interactions request's terms."""

    generation_config: JsonObject = field(default_factory=dict)
    media_resolution: str | None = None
    service_tier: str | None = None


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def interaction_generation_settings(
    fields: Mapping[str, object], *, model: str
) -> InteractionGenerationSettings:
    """Map ``GenerateContentConfig`` fields onto an Interactions request.

    Called once, when the client is built, so a parameter with no equivalent
    fails at startup naming the model, as an unknown one already does.
    """
    config = types.GenerateContentConfig.model_validate(dict(fields))
    generation: JsonObject = {}
    for name in _SAME_NAMED_GENERATION_FIELDS:
        value = getattr(config, name)
        if value is not None:
            generation[name] = cast("JsonValue", value)
    unmapped: list[str] = []
    thinking = config.thinking_config
    if thinking is not None:
        if thinking.thinking_level is not None:
            generation["thinking_level"] = _enum_value(thinking.thinking_level).lower()
        if thinking.include_thoughts:
            generation["thinking_summaries"] = "auto"
        if thinking.thinking_budget is not None:
            unmapped.append("thinking_config.thinking_budget")
    media_resolution = None
    if config.media_resolution is not None:
        media_resolution = (
            _enum_value(config.media_resolution)
            .removeprefix("MEDIA_RESOLUTION_")
            .lower()
        )
    service_tier = (
        _enum_value(config.service_tier).lower()
        if config.service_tier is not None
        else None
    )
    handled = {
        *_SAME_NAMED_GENERATION_FIELDS,
        "thinking_config",
        "media_resolution",
        "service_tier",
    }
    unmapped.extend(
        sorted(
            name
            for name in config.model_fields_set
            if name not in handled and getattr(config, name) is not None
        )
    )
    if unmapped:
        raise ValueError(
            f"llm_parameters for Gemini model '{model}' set {', '.join(unmapped)}, "
            "which the Interactions API has no equivalent for. Remove it, or set "
            "use_interactions_api: false to keep the model on generateContent."
        )
    return InteractionGenerationSettings(
        generation_config=generation,
        media_resolution=media_resolution,
        service_tier=service_tier,
    )


class InteractionStepAccumulator:
    """Folds a streamed interaction's events back into whole steps.

    The completion event carries usage but no steps, so a streamed response is
    reconstructed from ``step.start`` / ``step.delta`` / ``step.stop``. Each
    step comes back in the shape a non-streamed response has, which is the
    shape the API accepts when it is replayed.
    """

    def __init__(self) -> None:
        self._steps: dict[int, JsonObject] = {}
        self._arguments: dict[int, list[str]] = {}

    def start(self, index: int, step: JsonObject) -> None:
        """Begin the step at *index*."""
        self._steps[index] = dict(step)

    def delta(self, index: int, delta: JsonObject) -> str | None:
        """Fold *delta* into its step; returns any text to show the user."""
        step = self._steps.setdefault(index, {})
        kind = delta.get("type")
        if kind == "arguments_delta":
            chunk = delta.get("arguments")
            if isinstance(chunk, str):
                self._arguments.setdefault(index, []).append(chunk)
            return None
        if kind == "thought_signature":
            step["signature"] = delta.get("signature")
            return None
        if kind == "thought_summary":
            content = delta.get("content")
            if isinstance(content, dict):
                _append_text(step, "summary", content.get("text"))
            return None
        if kind == "text_annotation_delta":
            annotations = delta.get("annotations")
            content = step.get("content")
            if isinstance(annotations, list) and isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    existing = last.setdefault("annotations", [])
                    if isinstance(existing, list):
                        existing.extend(annotations)
            return None
        if kind == "text":
            text = delta.get("text")
            _append_text(step, "content", text)
            return text if isinstance(text, str) else None
        if kind in _CONTENT_DELTA_KINDS:
            content = step.setdefault("content", [])
            if isinstance(content, list):
                content.append(dict(delta))
            return None
        step.update({key: value for key, value in delta.items() if key != "type"})
        return None

    def stop(self, index: int) -> JsonObject:
        """Finish the step at *index* and return it."""
        step = self._steps.setdefault(index, {})
        chunks = self._arguments.pop(index, None)
        if chunks:
            joined = "".join(chunks)
            step["arguments"] = cast("JsonValue", json.loads(joined)) if joined else {}
        return step

    @property
    def steps(self) -> list[JsonObject]:
        """Every step so far, in the order the API numbered them."""
        return [self._steps[index] for index in sorted(self._steps)]


def _append_text(step: JsonObject, key: str, text: object) -> None:
    if not isinstance(text, str):
        return
    blocks = step.setdefault(key, [])
    if not isinstance(blocks, list):
        return
    if blocks and isinstance(blocks[-1], dict) and blocks[-1].get("type") == "text":
        previous = blocks[-1].get("text")
        blocks[-1]["text"] = f"{previous if isinstance(previous, str) else ''}{text}"
    else:
        blocks.append({"type": "text", "text": text})


@dataclass(frozen=True)
class InteractionOutput:
    """What a response's steps say, in the chat loop's terms."""

    content: str | None
    tool_calls: list[tuple[str, str, dict[str, object]]]
    thought_summaries: list[str]


def read_steps(steps: Sequence[JsonObject]) -> InteractionOutput:
    """The reply text, tool calls and thought summaries in *steps*."""
    texts: list[str] = []
    calls: list[tuple[str, str, dict[str, object]]] = []
    summaries: list[str] = []
    for step in steps:
        kind = step.get("type")
        if kind == "model_output":
            for block in _blocks(step.get("content")):
                text = block.get("text")
                if block.get("type") == "text" and isinstance(text, str):
                    texts.append(text)
        elif kind == "function_call":
            call_id, name, arguments = (
                step.get("id"),
                step.get("name"),
                step.get("arguments"),
            )
            if isinstance(call_id, str) and isinstance(name, str):
                calls.append((
                    call_id,
                    name,
                    dict(arguments) if isinstance(arguments, dict) else {},
                ))
        elif kind == "thought":
            for block in _blocks(step.get("summary")):
                text = block.get("text")
                if isinstance(text, str):
                    summaries.append(text)
    return InteractionOutput(
        content="".join(texts) or None, tool_calls=calls, thought_summaries=summaries
    )


def _blocks(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [block for block in value if isinstance(block, dict)]


def history_steps(
    messages: Sequence[LLMMessage],
    to_contents: Callable[[list[LLMMessage]], list[types.Content]],
    *,
    media_resolution: str | None,
) -> list[JsonObject]:
    """The Interactions input for *messages*.

    Assistant messages with stored replay steps are rendered from them and the
    message; runs of everything else go through *to_contents* (the client's own
    message conversion) and are translated.
    """
    steps: list[JsonObject] = []
    pending: list[LLMMessage] = []

    def flush() -> None:
        if pending:
            steps.extend(
                contents_to_steps(
                    to_contents(list(pending)), media_resolution=media_resolution
                )
            )
            pending.clear()

    for message in messages:
        stored = stored_replay_steps(message)
        if stored is None or not isinstance(message, AssistantMessage):
            pending.append(message)
            continue
        flush()
        steps.extend(_replayed(message, stored))
    flush()
    return _named_results(steps)


def _named_results(steps: list[JsonObject]) -> list[JsonObject]:
    """*steps* with every tool result named after the call it answers.

    The call id is what binds a result to its call, but Interactions also
    requires the result's name and rejects one that is missing or differs from
    the call's. A tool message does not always carry the name the model used,
    so the call's own name, from the same input, is the one sent.
    """
    names = {
        step["id"]: step["name"]
        for step in steps
        if step.get("type") == "function_call"
        and isinstance(step.get("id"), str)
        and isinstance(step.get("name"), str)
    }
    named: list[JsonObject] = []
    for step in steps:
        call_id = step.get("call_id")
        if (
            step.get("type") == "function_result"
            and isinstance(call_id, str)
            and call_id in names
        ):
            named.append({**step, "name": names[call_id]})
        else:
            named.append(step)
    return named
