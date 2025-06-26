"""
Factory for creating appropriate LLM clients based on model configuration.
"""

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface

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

    # Provider classes will be imported on demand to avoid circular imports
    _provider_classes: dict[str, str] = {
        "openai": "family_assistant.llm.providers.openai_client.OpenAIClient",
        "google": "family_assistant.llm.providers.google_genai_client.GoogleGenAIClient",
        "litellm": "family_assistant.llm.LiteLLMClient",
    }

    @classmethod
    def create_client(
        cls,
        config: dict[str, Any],
    ) -> "LLMInterface":
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
                - model_parameters: Pattern-based parameters (optional)
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
        provider = config.get("provider")
        if not provider:
            provider = cls._determine_provider(model)

        if provider not in cls._provider_classes:
            raise ValueError(f"Unknown provider: {provider} for model: {model}")

        # Get API key
        api_key = config.get("api_key")
        if not api_key and provider != "litellm":
            api_key = cls._get_api_key_for_provider(provider)

        # Extract provider-specific parameters
        # Remove keys that are handled separately
        provider_params = {
            k: v for k, v in config.items() if k not in ["model", "provider", "api_key"]
        }

        # Get model_parameters from llm_parameters config
        model_parameters = config.get("model_parameters", {})

        # Import the provider class dynamically
        client_class_path = cls._provider_classes[provider]
        module_path, class_name = client_class_path.rsplit(".", 1)

        # Import the module and get the class
        import importlib

        module = importlib.import_module(module_path)
        client_class = getattr(module, class_name)

        logger.info(f"Creating {class_name} for model: {model}")

        if provider == "litellm":
            # LiteLLMClient has a different constructor signature
            return client_class(
                model=model,
                model_parameters=model_parameters,
                **provider_params,
            )
        else:
            # Direct provider clients (OpenAI, Google)
            return client_class(
                api_key=api_key,
                model=model,
                model_parameters=model_parameters,
                **provider_params,
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
            raise ValueError(
                f"No environment variable mapping for provider: {provider}"
            )

        api_key = os.getenv(env_var)
        if not api_key:
            raise ValueError(f"API key not found in environment: {env_var}")

        return api_key
