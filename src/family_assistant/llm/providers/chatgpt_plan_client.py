"""OpenAI models billed to a ChatGPT plan through Sign in with ChatGPT.

The route is the public Responses API with a plan-scoped OAuth token, under a
few extra rules (https://developers.openai.com/siwc/token-sharing-open-source/
preview-limitations): every request is ``store: false`` and ``stream: true``,
system items are refused, several request fields are refused, and there is no
Chat Completions. This client is the OpenAI client with those rules applied;
see ``docs/design/chatgpt-plan-usage.md``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from family_assistant.llm import JsonObject, StructuredOutputError
from family_assistant.llm.connections import (
    ConnectionKey,
    PlanCredentialError,
    ProviderConnections,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    MessageReasoningInfo,
    UserMessage,
)
from family_assistant.llm.providers.openai_client import OpenAIClient

from ..base import (
    AuthenticationError,
    InvalidRequestError,
    LLMProviderError,
    ProviderConnectionError,
    RateLimitError,
    ServiceUnavailableError,
)

if TYPE_CHECKING:
    from openai.types.responses import Response

    from family_assistant.tools.types import ToolDefinition

logger = logging.getLogger(__name__)

_API_BASE_URL = "https://api.openai.com/v1"
_USAGE_URL = "https://chatgpt.com/settings/usage"
_TOOL_NAMESPACE = "family_assistant"

# Request fields the route refuses. `max_tokens` is here because this client
# sends it as `max_output_tokens`; `store` because it may only be false;
# `service_tier` because a price class is an API-billing concept.
_UNSUPPORTED_PARAMETERS = frozenset({
    "background",
    "conversation",
    "max_output_tokens",
    "max_tokens",
    "max_tool_calls",
    "metadata",
    "moderation",
    "multi_agent",
    "previous_response_id",
    "prompt",
    "prompt_cache_retention",
    "safety_identifier",
    "service_tier",
    "temperature",
    "top_logprobs",
    "top_p",
    "truncation",
    "user",
})

_ERROR_CODES: Mapping[str, type[LLMProviderError]] = {
    "subscription_sharing_usage_limit_exceeded": RateLimitError,
    "subscription_sharing_usage_unavailable": ServiceUnavailableError,
    "subscription_sharing_user_unavailable": ServiceUnavailableError,
    "subscription_sharing_user_not_eligible": AuthenticationError,
    "subscription_sharing_invalid_user": AuthenticationError,
    "chatpass_v2_scope_not_authorized": AuthenticationError,
    "chatpass_v2_invalid_authorization_context": AuthenticationError,
    "subscription_sharing_unsupported_capability": InvalidRequestError,
    "subscription_sharing_route_not_supported": InvalidRequestError,
}


class ChatGPTPlanClient(OpenAIClient):
    """An OpenAI model whose requests draw on the signed-in ChatGPT plan."""

    _provider_name = "chatgpt"

    def __init__(
        self,
        api_key: str,
        model: str,
        model_parameters: dict[str, dict[str, object]] | None = None,
        connections: ProviderConnections | None = None,
        **kwargs: Any,  # noqa: ANN401 # Same pass-through parameters as OpenAIClient
    ) -> None:
        del api_key  # the plan's OAuth token is the credential
        if connections is None or connections.chatgpt_plan is None:
            raise ValueError(
                f"Model '{model}' cannot run on the ChatGPT plan without the "
                "running application's ChatGPT sign-in."
            )
        if "base_url" in kwargs:
            raise ValueError(
                f"Model '{model}' points at another endpoint, which the ChatGPT "
                "plan does not serve."
            )
        self._token_source = connections.chatgpt_plan
        super().__init__(
            api_key="",
            model=model,
            model_parameters=model_parameters,
            connections=connections,
            **kwargs,
        )
        self._reject_unsupported_parameters()

    @staticmethod
    def _sdk_client(
        api_key: str,
        base_url: str | None,
        model: str,
        connections: ProviderConnections | None,
    ) -> AsyncOpenAI:
        del api_key, base_url, model
        assert connections is not None and connections.chatgpt_plan is not None
        source = connections.chatgpt_plan
        return connections.get(
            ConnectionKey("chatgpt", _API_BASE_URL, ""),
            # The SDK awaits this before every request, outside its retry
            # loop, so a credential failure surfaces as itself.
            lambda: AsyncOpenAI(
                api_key=source.access_token,
                base_url=_API_BASE_URL,
                # A usage-limit 429 does not clear in seconds; the retrying
                # client's fallback is the useful response to it.
                max_retries=0,
            ),
            AsyncOpenAI.close,
        )

    def _reject_unsupported_parameters(self) -> None:
        configured = {
            **self.default_kwargs,
            **self._resolve_model_parameters(self.model),
        }
        refused = sorted(key for key in configured if key in _UNSUPPORTED_PARAMETERS)
        if configured.get("store") is True:
            refused.append("store")
        if configured.get("use_responses_api") is False:
            refused.append("use_responses_api")
        if refused:
            raise ValueError(
                f"the ChatGPT plan does not accept {', '.join(refused)}, which its "
                "llm_parameters set"
            )

    def _uses_responses_api(self) -> bool:
        return True

    def _build_responses_params(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None,
        tool_choice: str | None,
        *,
        stream: bool,
    ) -> dict[str, object]:
        """The OpenAI request shape, with the route's rules applied.

        ``stream`` still chooses the cache mode, as on direct OpenAI; the
        request itself is always streamed, and a non-streaming caller collects
        it in :meth:`_create_response`.
        """
        params = super()._build_responses_params(
            messages, tools, tool_choice, stream=stream
        )
        params["stream"] = True
        params["store"] = False
        declared = params.get("tools")
        namespaced = (
            {tool.get("name") for tool in cast("list[dict[str, object]]", declared)}
            if isinstance(declared, list)
            else set()
        )
        input_items = params.get("input")
        if isinstance(input_items, list):
            params["input"] = [
                self._plan_input_item(item, namespaced)
                for item in cast("list[object]", input_items)
            ]
        if isinstance(declared, list) and declared:
            # The route takes function tools only inside a namespace (or as
            # `additional_tools` items, which activations already use).
            params["tools"] = [
                {
                    "type": "namespace",
                    "name": _TOOL_NAMESPACE,
                    "description": "Family Assistant's tools.",
                    "tools": declared,
                }
            ]
        return params

    @staticmethod
    def _plan_input_item(item: object, namespaced: set[object]) -> object:
        """An input item in the route's shape.

        System items are refused, so they go as developer messages. A replayed
        call to a tool declared in the namespace names it, as the model's own
        call did; a call to a tool activated through ``additional_tools`` was
        standalone and stays so.
        """
        if not isinstance(item, dict):
            return item
        typed = cast("dict[str, object]", item)
        if typed.get("role") == "system":
            return {**typed, "role": "developer"}
        if typed.get("type") == "function_call" and typed.get("name") in namespaced:
            return {**typed, "namespace": _TOOL_NAMESPACE}
        return typed

    async def _create_response(self, params: dict[str, object]) -> Response:
        """Stream the request and return the response it terminates with."""
        stream = await cast("Any", self.client.responses.create)(**{
            **params,
            "stream": True,
        })
        async for event in stream:
            if event.type in {"response.completed", "response.incomplete"}:
                # An incomplete run is returned like a direct one; the caller
                # records the truncation.
                return event.response
            if event.type == "response.failed":
                raise self._failed_response_error(event.response)
            if event.type == "error":
                raise self._error_from_code(event.code, event.message)
        raise ProviderConnectionError(
            "ChatGPT plan stream ended without a terminal event",
            provider=self._provider_name,
            model=self.model,
        )

    def _failed_response_error(self, response: object) -> LLMProviderError:
        error = getattr(response, "error", None)
        return self._error_from_code(
            getattr(error, "code", None),
            getattr(error, "message", None) or "ChatGPT plan request failed",
        )

    def _error_from_code(self, code: object, message: str) -> LLMProviderError:
        error_class = _ERROR_CODES.get(code) if isinstance(code, str) else None
        if error_class is RateLimitError:
            message = (
                f"{message} -- the ChatGPT plan's usage limit is reached; see "
                f"{_USAGE_URL}"
            )
        return (error_class or LLMProviderError)(
            message, provider=self._provider_name, model=self.model
        )

    def _error_type_for_code(self, code: str | None) -> str | None:
        error_class = _ERROR_CODES.get(code) if code else None
        return error_class.__name__ if error_class else None

    def _typed_error_for(self, error: Exception) -> LLMProviderError | None:
        if isinstance(error, PlanCredentialError):
            error_class = (
                ProviderConnectionError if error.transient else AuthenticationError
            )
            return error_class(
                str(error), provider=self._provider_name, model=self.model
            )
        if not isinstance(error, openai.APIStatusError):
            return None
        if error.status_code == 401:
            self._token_source.reject_access_token()
        typed = self._error_from_code(error.code, str(error))
        if type(typed) is not LLMProviderError:
            return typed
        if error.status_code == 401:
            return AuthenticationError(
                str(error), provider=self._provider_name, model=self.model
            )
        return None

    def _structured_usage(self, response: object) -> MessageReasoningInfo | None:
        return self._responses_reasoning_info(cast("Response", response))

    async def _structured_attempt(
        self,
        messages: Sequence[LLMMessage],
        *,
        text_format: type[BaseModel] | None,
    ) -> tuple[object, str]:
        """One structured request; returns the parsed output and its raw text."""
        params = self._build_responses_params(
            self._process_tool_messages(list(messages)), None, None, stream=False
        )
        params.pop("stream")
        if text_format is None:
            params["text"] = {"format": {"type": "json_object"}}

        async def request() -> Any:  # noqa: ANN401 - the SDK's stream helper is untyped through cast
            manager = cast("Any", self.client.responses.stream)(
                **params, **({"text_format": text_format} if text_format else {})
            )
            async with manager as stream:
                async for event in stream:
                    if event.type == "response.failed":
                        raise self._failed_response_error(event.response)
                    if event.type == "error":
                        raise self._error_from_code(event.code, event.message)
                return await stream.get_final_response()

        response = await self._instrumented_structured_request(
            messages,
            request,
            response_schema=(
                text_format.model_json_schema() if text_format else params["text"]
            ),
        )
        parsed = getattr(response, "output_parsed", None) if text_format else None
        return parsed, response.output_text or ""

    async def _with_schema_retries[R](
        self,
        messages: Sequence[LLMMessage],
        max_retries: int,
        attempt: Callable[[Sequence[LLMMessage]], Awaitable[tuple[R | None, str]]],
        correction: str,
    ) -> R:
        attempt_messages: list[LLMMessage] = list(messages)
        raw_response: str | None = None
        last_error: Exception | None = None
        for attempt_number in range(max_retries + 1):
            try:
                result, raw_response = await attempt(attempt_messages)
                if result is None:
                    raise ValueError("LLM returned no parsed structured output")
                return result
            except (ValidationError, json.JSONDecodeError, ValueError, TypeError) as e:
                last_error = e
                logger.warning(
                    "ChatGPT plan structured output invalid (attempt %s/%s): %s",
                    attempt_number + 1,
                    max_retries + 1,
                    e,
                )
                attempt_messages.append(AssistantMessage(content=raw_response or ""))
                attempt_messages.append(UserMessage(content=f"{correction} Error: {e}"))
            except Exception as e:
                raise self._map_error_to_typed_exception(e) from e
        raise StructuredOutputError(
            message=(
                f"Failed to generate valid structured output after {max_retries + 1} "
                "attempts"
            ),
            provider=self._provider_name,
            model=self.model,
            raw_response=raw_response,
            validation_error=last_error,
        )

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Structured output through Responses ``text.format`` (no Chat Completions)."""
        self._validate_user_input(messages)

        async def attempt(
            attempt_messages: Sequence[LLMMessage],
        ) -> tuple[T | None, str]:
            parsed, raw = await self._structured_attempt(
                attempt_messages, text_format=response_model
            )
            if parsed is not None:
                return cast("T", parsed), raw
            return (
                response_model.model_validate_json(
                    self._extract_json_from_response(raw)
                )
                if raw
                else None
            ), raw

        return await self._with_schema_retries(
            messages,
            max_retries,
            attempt,
            "Your previous response did not satisfy the required schema. Please try "
            "again with valid structured output.",
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """A JSON object through Responses JSON mode."""
        self._validate_user_input(messages)
        instructed = self._add_system_instruction(
            messages,
            "You must respond with a valid JSON object. "
            "Respond ONLY with the JSON object and no additional text.",
        )

        async def attempt(
            attempt_messages: Sequence[LLMMessage],
        ) -> tuple[JsonObject | None, str]:
            _, raw = await self._structured_attempt(attempt_messages, text_format=None)
            if not raw:
                raise ValueError("LLM returned empty content")
            return self._parse_json_object_response(raw), raw

        return await self._with_schema_retries(
            instructed,
            max_retries,
            attempt,
            "Your previous response was not a valid JSON object. Please try again "
            "and respond with JSON only.",
        )
