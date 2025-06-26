"""
Factory for creating appropriate LLM clients based on model configuration.
"""

import logging
import os
from typing import Any

from family_assistant.llm import LLMInterface

from .providers.google_genai_client import GoogleGenAIClient
from .providers.openai_client import OpenAIClient

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

    _provider_classes: dict[str, type[LLMInterface]] = {
        "openai": OpenAIClient,
        "google": GoogleGenAIClient,
    }

    @classmethod
    def create_client(
        cls,
        config: dict[str, Any],
        use_direct_providers: bool = False,
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
                - model_parameters: Pattern-based parameters (optional)
                - Additional provider-specific parameters
            use_direct_providers: If True, use direct provider implementations.
                                If False, use LiteLLM (default for backward compatibility)

        Returns:
            Instantiated LLM client

        Raises:
            ValueError: If model/provider is not recognized
        """
        model = config.get("model")
        if not model:
            raise ValueError("Model must be specified in config")

        # If not using direct providers, return LiteLLM client
        if not use_direct_providers:
            logger.info(f"Creating LiteLLM client for model: {model}")
            return cls._create_litellm_client(config)

        # Determine provider - explicit config takes precedence
        provider = config.get("provider")
        if not provider:
            provider = cls._determine_provider(model)

        if provider not in cls._provider_classes:
            # Fall back to LiteLLM for unsupported providers
            logger.warning(
                f"Provider '{provider}' not implemented in direct mode, falling back to LiteLLM"
            )
            return cls._create_litellm_client(config)

        # Get API key
        api_key = config.get("api_key")
        if not api_key:
            api_key = cls._get_api_key_for_provider(provider)

        # Extract provider-specific parameters
        # Remove keys that are handled separately
        provider_params = {
            k: v
            for k, v in config.items()
            if k not in ["model", "provider", "api_key", "model_parameters"]
        }

        # Get model-specific parameters
        model_parameters = config.get("model_parameters")

        # Instantiate client
        client_class = cls._provider_classes[provider]
        logger.info(f"Creating {client_class.__name__} for model: {model}")

        # Type checker workaround - it can't infer the specific class from dict lookup
        if provider == "openai":
            return OpenAIClient(
                api_key=api_key,
                model=model,
                model_parameters=model_parameters,
                **provider_params,
            )
        elif provider == "google":
            return GoogleGenAIClient(
                api_key=api_key,
                model=model,
                model_parameters=model_parameters,
                **provider_params,
            )
        else:
            # This should never happen due to earlier checks
            raise ValueError(f"Unknown provider: {provider}")

    @classmethod
    def _create_litellm_client(cls, config: dict[str, Any]) -> LLMInterface:
        """Create a LiteLLM client from config."""
        # Import LiteLLMClient here to avoid circular imports
        from family_assistant.llm import LiteLLMClient

        model = config["model"]
        model_parameters = config.get("model_parameters")

        # Extract other parameters for LiteLLM
        kwargs = {
            k: v
            for k, v in config.items()
            if k not in ["model", "model_parameters", "provider", "api_key"]
        }

        return LiteLLMClient(
            model=model,
            model_parameters=model_parameters,
            **kwargs,
        )

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

        # Check for router-style prefixes (e.g., "openrouter/openai/gpt-4")
        if model.count("/") >= 2:
            parts = model.split("/")
            # Try second part as provider
            if parts[1] in cls._provider_classes:
                return parts[1]

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
            raise ValueError(
                f"No environment variable mapping for provider: {provider}"
            )

        api_key = os.getenv(env_var)
        if not api_key:
            raise ValueError(f"API key not found in environment: {env_var}")

        return api_key
