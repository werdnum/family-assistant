# LLM Provider Migration Plan: From LiteLLM to Direct Libraries

## Status

- **Phase 1: Parallel Implementation** ✅ COMPLETED (2025-06-26)
  - OpenAI provider implemented
  - Google Gemini provider implemented (using new google-genai SDK)
  - Feature flag `use_direct_providers` added
  - Ready for testing
- **Phase 2: Testing** 🚧 IN PROGRESS
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

```
src/family_assistant/llm/
├── __init__.py              # Re-exports for backward compatibility
├── base.py                  # Protocol, data classes, exceptions
├── factory.py               # Provider selection and instantiation
├── retrying_client.py       # Retry, fallback, and ID translation
├── providers/
│   ├── __init__.py
│   ├── openai_client.py
│   ├── google_genai_client.py
│   └── anthropic_client.py
└── utils/
    ├── __init__.py
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
    
    def __init__(self, api_key: str, model: str, **kwargs):
        genai.configure(api_key=api_key)
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
                - provider: Explicit provider name (optional)
                - api_key: API key (optional, will use env var if not provided)
                - Additional provider-specific parameters
            
        Returns:
            Instantiated LLM client
            
        Raises:
            ValueError: If model/provider is not recognized
        """
        model = config.get("model")
        if not model:
            raise ValueError("Model must be specified in config")
        # Determine provider - explicit config takes precedence
        provider = config.get("provider")
        if not provider:
            provider = cls._determine_provider(model)
        
        if provider not in cls._provider_classes:
            raise ValueError(f"Unknown provider: {provider} for model: {model}")
        
        # Get API key
        api_key = config.get("api_key")
        if not api_key:
            api_key = cls._get_api_key_for_provider(provider)
        
        # Extract provider-specific parameters
        # Remove keys that are handled separately
        provider_params = {k: v for k, v in config.items() 
                          if k not in ["model", "provider", "api_key"]}
        
        # Instantiate client
        client_class = cls._provider_classes[provider]
        logger.info(f"Creating {client_class.__name__} for model: {model}")
        
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
        
        raise ValueError(f"Cannot determine provider for model: {model}")
    
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

### 4. Retrying Client (retrying_client.py)

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
            f"Failed {client_type} LLM request:
"
            f"Error: {error}
"
            f"Request: {json.dumps(request_info, indent=2)}"
        )
```

### 5. ID Translation (utils/id_translator.py)

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

### Phase 1: Parallel Implementation ✅ COMPLETED

Status: Completed on 2025-06-26

1. ✅ Created new directory structure alongside existing code
2. ✅ Implemented OpenAI and Google Gemini providers
3. ✅ Added feature flag to `Assistant.setup_dependencies()`:

```python
# In assistant.py
if self.config.get("use_direct_providers", False):
    # New implementation using existing LLM config format
    primary_config = {
        "model": profile_llm_model,
        **model_parameters  # Contains provider-specific params
    }
    primary_client = LLMClientFactory.create_client(primary_config)
    
    if fallback_model_id:
        fallback_config = {
            "model": fallback_model_id,
            **fallback_parameters
        }
        fallback_client = LLMClientFactory.create_client(fallback_config)
        
        llm_client = RetryingLLMClient(
            primary_client=primary_client,
            fallback_client=fallback_client,
            max_retries=retry_config.get("max_retries", 1)
        )
    else:
        llm_client = primary_client
else:
    # Keep existing LiteLLMClient
    llm_client = LiteLLMClient(...)
```

### Phase 2: Testing 🚧 IN PROGRESS

Current Status: Ready for testing with the following setup:

1. **Configuration**: Set `use_direct_providers: true` in `config.yaml`

2. **API Keys**: Set environment variables:

   - `OPENAI_API_KEY` for OpenAI models (gpt-4o, gpt-4, etc.)
   - `GEMINI_API_KEY` for Google models (gemini-2.0-flash-001, etc.)

3. **Model Selection**: Configure in service profiles, e.g.:

   ```yaml
   processing_config:
     llm_model: "gpt-4o"  # or "gemini-2.0-flash-001"
   ```

The testing strategy aligns with the existing testing infrastructure in the project.

#### Unit Tests

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

#### Integration Tests

1. **Provider Comparison Tests** (`tests/functional/llm/`)

   - Use pytest fixtures similar to existing patterns
   - Compare outputs between LiteLLM and direct implementations
   - Test with real API calls (using test API keys)
   - Verify tool calling compatibility

2. **End-to-End Tests**

   - Integrate with existing `test_processing_service` fixtures
   - Test complete conversation flows
   - Verify tool execution with different providers

#### Test Fixtures

```python
# tests/functional/llm/conftest.py
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
   - Include configuration examples
   - Document breaking changes (if any)
   - Provide rollback instructions

### Configuration Examples

```yaml
# config.yaml - Example configuration
use_direct_providers: true  # Feature flag

# Provider-specific configurations
llm_providers:
  default:
    model: "gpt-4"
    temperature: 0.7
    max_tokens: 4096
    
  fallback:
    model: "gemini-pro"
    provider: "google"  # Explicit provider
    temperature: 0.7
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

## Future Work

1. **Streaming Support**: Add streaming response capability when needed
2. **Additional Providers**: Add support for Cohere, AI21, etc. as required
3. **Provider-Specific Features**: Expose unique features like Anthropic's constitutional AI
4. **Performance Optimizations**: Connection pooling, request batching where supported
