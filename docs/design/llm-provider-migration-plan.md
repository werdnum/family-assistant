# LLM Provider Migration Plan: From LiteLLM to Direct Libraries

## Summary

This migration is partially implemented. The basic infrastructure is in place with OpenAI and Google
providers working, but critical components like retry/fallback logic and Anthropic support are
missing. The new approach uses a `provider` key in each model's configuration to select the
appropriate client (e.g., `openai`, `google-genai`, `litellm`), removing the global
`use_direct_providers` feature flag. This allows for greater flexibility and provider-specific
configurations, such as a custom `api_base` for Google GenAI.

## Status

- **Phase 1: Parallel Implementation** 🚧 PARTIALLY COMPLETED (2025-06-26)
  - ✅ Directory structure created (`src/family_assistant/llm/`)
  - ✅ Base components implemented (`base.py`)
  - ✅ OpenAI provider implemented (`providers/openai_client.py`)
  - ✅ Google GenAI provider implemented (`providers/google_genai_client.py`)
  - ✅ Factory pattern implemented (`factory.py`)
  - ❌ **REMOVED** Feature flag `use_direct_providers` integrated in `assistant.py`
  - ✅ **NEW** Provider-based client selection implemented in `assistant.py`
  - ❌ Anthropic provider NOT implemented
  - ❌ RetryingLLMClient with fallback NOT implemented
  - ❌ ID translation utilities NOT implemented
  - ❌ Converter utilities NOT implemented
- **Phase 2: Testing** ⏳ NOT STARTED
  - No test files created yet
  - Integration tests not implemented
- **Phase 3: Gradual Rollout** ⏳ NOT STARTED
- **Phase 4: Full Migration** ⏳ NOT STARTED

## Overview

This document outlines the plan to migrate from LiteLLM to direct provider libraries (OpenAI, Google
GenAI, Anthropic). The migration aims to reduce dependencies while maintaining clean architecture
and full functionality.

## Current State Analysis

### LiteLLM API Surface Used

The codebase uses a minimal subset of LiteLLM's features:

- `litellm.acompletion()` - Main completion API
- `litellm.file_upload()` - Gemini file uploads only
- Response parsing (choices, message content, tool calls)
- Exception types for error handling

### Key Findings

1. **OpenAI**: The native library is almost a drop-in replacement

   - Same message format
   - Same tool/function calling format
   - Nearly identical response structure

2. **Google GenAI**: Requires format conversion but straightforward

   - Message role mapping (user/assistant → user/model)
   - Tool definition format differences
   - Different response structure

3. **Anthropic**: Similar complexity to Google (not analyzed in detail yet)

## Architecture Design

### Core Principles

- One class per provider (no if-statements for provider selection)
- Clean separation of concerns
- Tool-calling loop remains in `ProcessingService`
- Provider-agnostic resilience layer

### Class Structure

#### Implemented:

```
src/family_assistant/llm/
├── __init__.py              # Re-exports for backward compatibility
├── base.py                  # Protocol, data classes, exceptions
├── factory.py               # Provider selection and instantiation
├── providers/
│   ├── __init__.py
│   ├── openai_client.py
│   └── google_genai_client.py
└── utils/
    └── __init__.py
```

#### Not Yet Implemented:

```
src/family_assistant/llm/
├── retrying_client.py       # Retry, fallback, and ID translation
├── providers/
│   └── anthropic_client.py
└── utils/
    ├── converters.py        # Format conversion utilities
    └── id_translator.py     # Cross-provider tool call ID management
```

## Implementation Details

### 1. Base Components (base.py)

Keep existing protocol and data classes:

```python
from typing import Protocol, Any
from dataclasses import dataclass, field

@dataclass(frozen=True)
class ToolCallFunction:
    """Represents the function to be called in a tool call."""
    name: str
    arguments: str  # JSON string

@dataclass(frozen=True)
class ToolCallItem:
    """Represents a single tool call requested by the LLM."""
    id: str
    type: str  # Usually "function"
    function: ToolCallFunction

@dataclass
class LLMOutput:
    """Standardized output structure from an LLM call."""
    content: str | None = None
    tool_calls: list[ToolCallItem] | None = field(default=None)
    reasoning_info: dict[str, Any] | None = field(default=None)

class LLMInterface(Protocol):
    """Protocol defining the interface for LLM clients."""

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        ...

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        ...

class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""
    def __init__(self, message: str, provider: str, model: str):
        self.provider = provider
        self.model = model
        super().__init__(message)

class RateLimitError(LLMProviderError):
    """Raised when hitting provider rate limits."""
    pass

class AuthenticationError(LLMProviderError):
    """Raised when authentication fails."""
    pass

class ModelNotFoundError(LLMProviderError):
    """Raised when the requested model doesn't exist."""
    pass

class ContextLengthError(LLMProviderError):
    """Raised when exceeding model context limits."""
    pass

class InvalidRequestError(LLMProviderError):
    """Raised when the request format is invalid."""
    pass
```

### 2. Provider Implementations

#### OpenAI Client (openai_client.py)

```python
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
import logging

from ..base import LLMInterface, LLMOutput, ToolCallItem, ToolCallFunction, LLMProviderError

logger = logging.getLogger(__name__)

class OpenAIClient(LLMInterface):
    """Direct OpenAI API implementation."""

    def __init__(self, api_key: str, model: str, **kwargs):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.default_params = kwargs
        logger.info(f"OpenAIClient initialized for model: {model}")

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using OpenAI API."""
        try:
            params = {
                "model": self.model,
                "messages": messages,
                **self.default_params
            }

            if tools:
                params["tools"] = tools
                params["tool_choice"] = tool_choice

            response = await self.client.chat.completions.create(**params)

            # Parse response - very similar to current LiteLLM parsing
            message = response.choices[0].message
            content = message.content

            tool_calls = None
            if message.tool_calls:
                tool_calls = [
                    ToolCallItem(
                        id=tc.id,
                        type=tc.type,
                        function=ToolCallFunction(
                            name=tc.function.name,
                            arguments=tc.function.arguments
                        )
                    )
                    for tc in message.tool_calls
                ]

            reasoning_info = None
            if response.usage:
                reasoning_info = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            return LLMOutput(
                content=content,
                tool_calls=tool_calls,
                reasoning_info=reasoning_info
            )

        except Exception as e:
            logger.error(f"OpenAI API error: {e}", exc_info=True)
            raise LLMProviderError(str(e), provider="openai", model=self.model)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        """Format user message with optional file content."""
        # Implementation for base64 encoding images
        # Similar to current LiteLLM implementation
        pass
```

#### Google GenAI Client (google_genai_client.py)

```python
import google.generativeai as genai
from google.generativeai.types import GenerateContentResponse
import logging

from ..base import LLMInterface, LLMOutput, ToolCallItem, ToolCallFunction, LLMProviderError
from ..utils.converters import (
    convert_openai_messages_to_genai,
    convert_openai_tools_to_genai,
    convert_genai_function_calls_to_tool_calls
)

logger = logging.getLogger(__name__)

class GoogleGenAIClient(LLMInterface):
    """Direct Google GenAI implementation."""

    def __init__(self, api_key: str, model: str, api_base: str | None = None, **kwargs):
        genai.configure(api_key=api_key, client_options={"api_endpoint": api_base} if api_base else None)
        self.model_name = model
        self.model = genai.GenerativeModel(model)
        self.default_params = kwargs
        logger.info(f"GoogleGenAIClient initialized for model: {model}")

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using Google GenAI."""
        try:
            # Convert message format
            genai_messages = convert_openai_messages_to_genai(messages)

            # Convert tools if provided
            generation_config = {**self.default_params}
            if tools:
                genai_tools = convert_openai_tools_to_genai(tools)
                # Note: tool_choice mapping may need adjustment
            else:
                genai_tools = None

            # Generate response
            response = await self.model.generate_content_async(
                genai_messages,
                tools=genai_tools,
                generation_config=generation_config
            )

            # Parse response
            content = response.text if response.text else None

            tool_calls = None
            if response.candidates and response.candidates[0].function_calls:
                tool_calls = convert_genai_function_calls_to_tool_calls(
                    response.candidates[0].function_calls
                )

            reasoning_info = None
            if hasattr(response, 'usage_metadata'):
                reasoning_info = {
                    "prompt_tokens": response.usage_metadata.prompt_token_count,
                    "completion_tokens": response.usage_metadata.candidates_token_count,
                    "total_tokens": response.usage_metadata.total_token_count,
                }

            return LLMOutput(
                content=content,
                tool_calls=tool_calls,
                reasoning_info=reasoning_info
            )

        except Exception as e:
            logger.error(f"Google GenAI API error: {e}", exc_info=True)
            raise LLMProviderError(str(e), provider="google", model=self.model_name)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        """Format user message with optional file content."""
        # Use native genai.upload_file() for file handling
        pass
```

### 3. Factory Pattern (factory.py)

```python
import os
from typing import Type, Any
import logging

from .base import LLMInterface
from .providers.openai_client import OpenAIClient
from .providers.google_genai_client import GoogleGenAIClient
from .providers.anthropic_client import AnthropicClient
from ..llm import LiteLLMClient

logger = logging.getLogger(__name__)

class LLMClientFactory:
    """
    Factory for creating appropriate LLM clients based on model configuration.

    This leverages the existing LLM configuration mechanism in the project,
    where provider can be determined from the model config or inferred from
    the model name.
    """

    # Provider mappings - can be extended
    _provider_prefixes = {
        "gpt-": "openai",
        "o1-": "openai",
        "o3-": "openai",
        "gemini-": "google",
        "claude-": "anthropic",
    }

    _provider_classes: dict[str, Type[LLMInterface]] = {
        "openai": OpenAIClient,
        "google": GoogleGenAIClient,
        "anthropic": AnthropicClient,
        "litellm": LiteLLMClient,
    }

    @classmethod
    def create_client(
        cls,
        config: dict[str, Any]
    ) -> LLMInterface:
        """
        Create appropriate LLM client based on configuration.

        This method accepts the same configuration format used in the existing
        LLM configuration system, making it a drop-in replacement.

        Args:
            config: LLM configuration dict containing:
                - model: Model identifier (required)
                - provider: Explicit provider name (optional, defaults to litellm)
                - api_key: API key (optional, will use env var if not provided)
                - api_base: API base URL (optional, for custom endpoints)
                - Additional provider-specific parameters

        Returns:
            Instantiated LLM client

        Raises:
            ValueError: If model/provider is not recognized
        """
        model = config.get("model")
        if not model:
            raise ValueError("Model must be specified in config")
        # Determine provider - explicit config takes precedence, defaults to litellm
        provider = config.get("provider", "litellm")

        if provider not in cls._provider_classes:
            raise ValueError(f"Unknown provider: {provider} for model: {model}")

        # Get API key
        api_key = config.get("api_key")
        if not api_key and provider != "litellm":
            api_key = cls._get_api_key_for_provider(provider)

        # Extract provider-specific parameters
        # Remove keys that are handled separately
        provider_params = {k: v for k, v in config.items()
                          if k not in ["model", "provider", "api_key"]}

        # Instantiate client
        client_class = cls._provider_classes[provider]
        logger.info(f"Creating {client_class.__name__} for model: {model}")

        if provider == "litellm":
            return client_class(model=model, **provider_params)
        else:
            return client_class(api_key=api_key, model=model, **provider_params)

    @classmethod
    def _determine_provider(cls, model: str) -> str:
        """Determine provider from model string."""
        # Check prefixes
        for prefix, provider in cls._provider_prefixes.items():
            if model.startswith(prefix):
                return provider

        # Check for explicit provider prefix (e.g., "openai/gpt-4")
        if "/" in model:
            provider, _ = model.split("/", 1)
            if provider in cls._provider_classes:
                return provider

        # Default to litellm if no other provider can be determined
        return "litellm"

    @classmethod
    def _get_api_key_for_provider(cls, provider: str) -> str:
        """Get API key from environment variables."""
        env_vars = {
            "openai": "OPENAI_API_KEY",
            "google": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }

        env_var = env_vars.get(provider)
        if not env_var:
            raise ValueError(f"No environment variable mapping for provider: {provider}")

        api_key = os.getenv(env_var)
        if not api_key:
            raise ValueError(f"API key not found in environment: {env_var}")

        return api_key
```

### 4. Retrying Client (retrying_client.py) - NOT YET IMPLEMENTED

```python
import asyncio
import logging
from typing import Any

from .base import LLMInterface, LLMOutput, LLMProviderError
from .utils.id_translator import ToolCallIDTranslator

logger = logging.getLogger(__name__)

class RetryingLLMClient(LLMInterface):
    """
    LLM client that provides retry and fallback capabilities:
    - Automatic retry with exponential backoff
    - Fallback to alternative providers
    - Cross-provider ID translation for tool continuity
    """

    def __init__(
        self,
        primary_client: LLMInterface,
        fallback_client: LLMInterface | None = None,
        max_retries: int = 1,
        initial_retry_delay: float = 1.0,
        backoff_factor: float = 2.0,
    ):
        self.primary_client = primary_client
        self.fallback_client = fallback_client
        self.max_retries = max_retries
        self.initial_retry_delay = initial_retry_delay
        self.backoff_factor = backoff_factor
        self._id_translator = ToolCallIDTranslator()

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response with retry and fallback logic."""
        last_error = None
        request_debug_info = {
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }

        # Try primary client with retries
        for attempt in range(self.max_retries + 1):
            try:
                # Translate any tool response IDs for primary provider
                translated_messages = self._id_translator.translate_messages(
                    messages, is_fallback=False
                )

                response = await self.primary_client.generate_response(
                    translated_messages, tools, tool_choice
                )

                # Normalize response IDs
                return self._id_translator.normalize_response(response, is_fallback=False)

            except LLMProviderError as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = self.initial_retry_delay * (self.backoff_factor ** attempt)
                    logger.warning(
                        f"Primary client failed (attempt {attempt + 1}), "
                        f"retrying in {delay}s: {e}"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Primary client failed after all retries: {e}")
                    self._dump_request_on_failure(request_debug_info, e, "primary")

        # Try fallback if available
        if self.fallback_client and last_error:
            logger.info("Attempting fallback client")
            try:
                # Translate messages for fallback provider
                translated_messages = self._id_translator.translate_messages(
                    messages, is_fallback=True
                )

                response = await self.fallback_client.generate_response(
                    translated_messages, tools, tool_choice
                )

                # Normalize response IDs
                return self._id_translator.normalize_response(response, is_fallback=True)

            except Exception as e:
                logger.error(f"Fallback client also failed: {e}", exc_info=True)
                self._dump_request_on_failure(request_debug_info, e, "fallback")
                # Re-raise the original error as it's likely more informative
                raise last_error

        # If we get here, primary failed and no fallback available
        if last_error:
            raise last_error
        else:
            raise LLMProviderError(
                "LLM request failed without specific error",
                provider="unknown",
                model="unknown"
            )

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        """Format user message with file - delegates to primary client."""
        return await self.primary_client.format_user_message_with_file(
            prompt_text, file_path, mime_type, max_text_length
        )

    def _dump_request_on_failure(
        self,
        request_info: dict[str, Any],
        error: Exception,
        client_type: str
    ) -> None:
        """Dump full request details on failure for debugging."""
        import json
        logger.error(
            f"Failed {client_type} LLM request:\n"
            f"Error: {error}\n"
            f"Request: {json.dumps(request_info, indent=2)}"
        )
```

### 5. ID Translation (utils/id_translator.py) - NOT YET IMPLEMENTED

```python
import hashlib
import uuid
from typing import Any

class ToolCallIDTranslator:
    """
    Manages translation between provider-specific and internal tool call IDs.
    This ensures tool call continuity when falling back between providers.
    """

    def __init__(self):
        # Maps internal IDs to provider-specific IDs
        self._id_mapping: dict[str, dict[str, Any]] = {}

    def normalize_response(self, response: LLMOutput, is_fallback: bool) -> LLMOutput:
        """Convert provider-specific IDs to internal IDs."""
        if not response.tool_calls:
            return response

        for tool_call in response.tool_calls:
            # Generate internal ID
            internal_id = f"call_{uuid.uuid4().hex[:8]}"

            # Store mapping
            self._id_mapping[internal_id] = {
                'provider_id': tool_call.id,
                'is_fallback': is_fallback,
            }

            # Replace with internal ID
            tool_call.id = internal_id

        return response

    def translate_messages(
        self,
        messages: list[dict[str, Any]],
        is_fallback: bool
    ) -> list[dict[str, Any]]:
        """Translate internal IDs in messages to provider-specific IDs."""
        translated = []

        for msg in messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                msg = self._translate_tool_response(msg, is_fallback)
            translated.append(msg)

        return translated

    def _translate_tool_response(
        self,
        msg: dict[str, Any],
        is_fallback: bool
    ) -> dict[str, Any]:
        """Translate tool response message IDs."""
        internal_id = msg["tool_call_id"]

        if internal_id not in self._id_mapping:
            # Unknown ID, pass through
            return msg

        mapping = self._id_mapping[internal_id]

        if mapping['is_fallback'] == is_fallback:
            # Same provider, use original ID
            return {**msg, "tool_call_id": mapping['provider_id']}
        else:
            # Different provider, need new ID
            # Generate deterministic ID based on internal ID
            new_id = self._generate_provider_id(internal_id, is_fallback)
            return {**msg, "tool_call_id": new_id}

    def _generate_provider_id(self, internal_id: str, is_fallback: bool) -> str:
        """Generate a provider-compatible ID."""
        # OpenAI expects "call_" prefix
        # Google/Anthropic are more flexible

        if not is_fallback:  # Assume primary is OpenAI-style
            # Create deterministic ID from internal ID
            hash_part = hashlib.md5(internal_id.encode()).hexdigest()[:24]
            return f"call_{hash_part}"
        else:
            # Fallback provider - use simpler format
            return f"fc_{internal_id}"
```

## Migration Strategy

### Phase 1: Parallel Implementation 🚧 PARTIALLY COMPLETED

Status: Partially completed on 2025-06-26

1. ✅ Created new directory structure alongside existing code
2. ✅ Implemented OpenAI and Google Gemini providers
3. ✅ Updated `Assistant.setup_dependencies()` to use the new provider-based factory:

**New Implementation in assistant.py:**

```python
# Simplified version without RetryingLLMClient (not yet implemented)
# The 'provider' key in the profile's processing_config determines the client.
# Defaults to 'litellm' if not specified.
client_config = {
    "model": profile_llm_model,
    "provider": profile_proc_conf_dict.get("provider", "litellm"),
    **self.config.get("llm_parameters", {}),
}

# Add any additional parameters from config
for key, value in profile_proc_conf_dict.items():
    if key.startswith("llm_") and key != "llm_model":
        client_config[key[4:]] = value

llm_client_for_profile = LLMClientFactory.create_client(
    config=client_config
)
```

**Missing Components for Complete Phase 1:**

- ❌ Anthropic provider implementation
- ❌ RetryingLLMClient for fallback support
- ❌ ID translation utilities for cross-provider tool continuity
- ❌ Converter utilities for message/tool format translation

### Phase 2: Testing ⏳ NOT STARTED

Current Status: Implementation can be tested, but no tests have been written yet. To test manually:

1. **Configuration**: In `config.yaml`, set the `provider` in the `processing_config` of a service
   profile.

2. **API Keys**: Set environment variables:

   - `OPENAI_API_KEY` for OpenAI models (gpt-4o, gpt-4, etc.)
   - `GEMINI_API_KEY` for Google models (gemini-2.0-flash-001, etc.)

3. **Model Selection**: Configure in service profiles, e.g.:

   ```yaml
   service_profiles:
     - id: "default_assistant"
       processing_config:
         provider: "openai"
         llm_model: "gpt-4o"
     - id: "gemini_assistant"
       processing_config:
         provider: "google"
         llm_model: "gemini-1.5-flash"
         api_base: "https://generativelanguage.googleapis.com/v1beta" # Example custom endpoint
     - id: "litellm_assistant"
       processing_config:
         provider: "litellm" # Explicitly use LiteLLM
         llm_model: "openrouter/anthropic/claude-3-haiku"
   ```

The testing strategy aligns with the existing testing infrastructure in the project. **Note: No
tests have been implemented yet.**

#### Unit Tests (TO BE IMPLEMENTED)

1. **Provider Client Tests** (`tests/unit/llm/providers/`)
   - Mock provider SDK responses
   - Test error handling and retries
   - Verify request/response transformations
   - Use existing `RuleBasedMockLLMClient` patterns
2. **Factory Tests** (`tests/unit/llm/test_factory.py`)
   - Test provider detection logic
   - Verify configuration parsing
   - Test error cases for unknown providers
3. **RetryingClient Tests** (`tests/unit/llm/test_retrying_client.py`)
   - Test retry logic with controlled failures
   - Verify fallback behavior
   - Test ID translation between providers
   - Ensure debug dumping works correctly

#### Integration Tests (TO BE IMPLEMENTED)

1. **Provider Comparison Tests** (`tests/functional/llm/`)
   - Use pytest fixtures similar to existing patterns
   - Compare outputs between LiteLLM and direct implementations
   - Test with real API calls (using test API keys)
   - Verify tool calling compatibility
2. **End-to-End Tests**
   - Integrate with existing `test_processing_service` fixtures
   - Test complete conversation flows
   - Verify tool execution with different providers

#### Test Fixtures (TO BE IMPLEMENTED)

```python
# tests/functional/llm/conftest.py (does not exist yet)
@pytest.fixture
def direct_llm_client(request):
    """Fixture providing direct LLM client based on test parameters."""
    provider = request.param
    config = {
        "model": TEST_MODELS[provider],
        "provider": provider,
        # Use test API keys from environment
    }
    return LLMClientFactory.create_client(config)

@pytest.fixture
def mock_llm_factory():
    """Factory fixture for creating mock LLM clients."""
    def _create_mock(provider: str, rules: list):
        # Create provider-specific mock using RuleBasedMockLLMClient pattern
        return ProviderMockAdapter(provider, rules)
    return _create_mock
```

### Phase 3: Gradual Rollout

1. Enable for development environments first
2. Run both implementations in parallel for comparison
3. Monitor for any behavioral differences
4. Gradually migrate production after validation

## Documentation Plan

### Code Documentation

1. **Module Docstrings**: Each new module includes comprehensive docstrings
2. **Type Hints**: Full type annotations for all public interfaces
3. **Example Usage**: Document common usage patterns in module docstrings

### User Documentation Updates

1. **Update `docs/user/USER_GUIDE.md`**:
   - Add section on provider configuration
   - Document environment variables for each provider
   - Include troubleshooting guide for common provider errors
2. **Update System Prompts** (if needed):
   - Review `prompts.yaml` for any LLM-specific references
   - Ensure prompts work well across all providers
3. **Migration Guide**:
   - Create `docs/migration/litellm-to-direct.md`
   * Include configuration examples
   * Document breaking changes (if any)
   * Provide rollback instructions

### Configuration Examples

```yaml
# config.yaml - Example configuration
service_profiles:
  - id: "default_assistant"
    processing_config:
      provider: "openai"
      llm_model: "gpt-4o"
      temperature: 0.7
      max_tokens: 4096
  - id: "gemini_assistant"
    processing_config:
      provider: "google"
      llm_model: "gemini-1.5-flash"
      api_base: "https://generativelanguage.googleapis.com/v1beta"
  - id: "litellm_assistant"
    processing_config:
      provider: "litellm"
      llm_model: "openrouter/anthropic/claude-3-haiku"
```

## Benefits

1. **Reduced Dependencies**: Remove LiteLLM dependency
2. **Better Control**: Direct access to provider-specific features
3. **Clearer Errors**: Provider-specific error messages
4. **Type Safety**: Better IDE support with provider SDKs
5. **Maintainability**: Clear separation of provider logic

## Risks and Mitigations

### Risk 1: Provider API Changes

**Mitigation**: Version pin provider libraries, monitor changelogs

### Risk 2: Missing Edge Cases

**Mitigation**: Comprehensive testing, gradual rollout, keep LiteLLM as fallback initially

### Risk 3: Cross-Provider Compatibility

**Mitigation**: ID translation layer, thorough testing of fallback scenarios

## Success Criteria

1. All existing functionality works identically
2. No regression in error handling
3. Performance equal or better than LiteLLM
4. Clean, maintainable code structure
5. Successful handling of cross-provider fallbacks

## Action Items to Complete Phase 1

1. **Implement Anthropic Provider** (`providers/anthropic_client.py`):
   - Create client using Anthropic SDK
   - Handle message format conversion
   - Implement tool calling support
2. **Implement RetryingLLMClient** (`retrying_client.py`):
   - Add retry logic with exponential backoff
   - Implement fallback to alternative providers
   - Integrate ID translation for cross-provider continuity
3. **Implement Converter Utilities** (`utils/converters.py`):
   - `convert_openai_messages_to_genai()` - Already referenced in Google client
   - `convert_openai_tools_to_genai()` - For tool definition conversion
   - `convert_genai_function_calls_to_tool_calls()` - For response parsing
   - Add similar converters for Anthropic
4. **Implement ID Translation** (`utils/id_translator.py`):
   - Create `ToolCallIDTranslator` class
   - Handle provider-specific ID formats
   - Ensure tool call continuity across provider fallbacks
5. **Update Factory to Use RetryingClient**:
   - Modify `assistant.py` to create RetryingLLMClient when fallback is configured
   - Ensure proper configuration parsing for retry parameters

## Future Work

1. **Streaming Support**: Add streaming response capability when needed
2. **Additional Providers**: Add support for Cohere, AI21, etc. as required
3. **Provider-Specific Features**: Expose unique features like Anthropic's constitutional AI
4. **Performance Optimizations**: Connection pooling, request batching where supported
