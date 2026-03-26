"""
Factory for creating appropriate LLM clients based on model configuration.
"""

import importlib
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
        "anthropic": "family_assistant.llm.providers.anthropic_client.AnthropicClient",
    }

    @classmethod
    def create_client(
        cls,
        # ast-grep-ignore: no-dict-any - LLM config dict has varying keys per provider/format
        config: dict[str, Any],
    ) -> "LLMInterface":
        """
        Create appropriate LLM client based on configuration.

        Args:
            config: LLM configuration dict containing either:
                Simple format:
                - model: Model identifier (required)
                - provider: Explicit provider name (optional, auto-detected from model)
                - api_key: API key (optional, will use env var if not provided)
                - api_base: API base URL (optional, for custom endpoints)
                - model_parameters: Pattern-based parameters (optional)
                - Additional provider-specific parameters

                Retry format:
                - retry_config: Dict with 'primary' and optional 'fallback' configs

        Returns:
            Instantiated LLM client

        Raises:
            ValueError: If model/provider is not recognized
        """
        # Check for retry configuration
        if "retry_config" in config:
            retry_config = config["retry_config"]

            # Create primary client
            primary_client = cls._create_single_client(retry_config["primary"])
            primary_model = retry_config["primary"]["model"]

            # Create fallback if specified
            fallback_client = None
            fallback_model = None
            if "fallback" in retry_config:
                fallback_client = cls._create_single_client(retry_config["fallback"])
                fallback_model = retry_config["fallback"]["model"]

            # Return retrying wrapper
            from .retrying_client import (  # noqa: PLC0415
                RetryingLLMClient,
            )

            return RetryingLLMClient(
                primary_client=primary_client,
                primary_model=primary_model,
                fallback_client=fallback_client,
                fallback_model=fallback_model,
            )

        # Simple configuration - create single client
        return cls._create_single_client(config)

    @classmethod
    # ast-grep-ignore: no-dict-any - LLM config dict has varying keys per provider/format
    def _create_single_client(cls, config: dict[str, Any]) -> "LLMInterface":
        """Create a single LLM client (existing logic)."""
        model = config.get("model")
        if not model:
            raise ValueError("Model must be specified in config")

        # Determine provider - explicit config takes precedence
        provider = config.get("provider")
        if not provider:
            provider = cls._determine_provider(model)
            logger.info(
                f"No explicit provider specified for model '{model}', "
                f"auto-determined provider: '{provider}'"
            )
        else:
            logger.info(
                f"Using explicitly configured provider '{provider}' for model '{model}'"
            )

        if provider not in cls._provider_classes:
            available_providers = list(cls._provider_classes.keys())
            raise ValueError(
                f"Unknown provider: '{provider}' for model: '{model}'. "
                f"Available providers: {available_providers}"
            )

        # Detect OpenRouter models and configure accordingly
        is_openrouter = model.startswith("openrouter/")

        # Get API key
        api_key = config.get("api_key")
        if not api_key:
            if is_openrouter:
                api_key = os.getenv("OPENROUTER_API_KEY", "")
                if not api_key:
                    raise ValueError(
                        "API key not found in environment: OPENROUTER_API_KEY"
                    )
            else:
                api_key = cls._get_api_key_for_provider(provider)

        # Extract provider-specific parameters
        # Remove keys that are handled separately
        provider_params = {
            k: v
            for k, v in config.items()
            if k not in {"model", "provider", "api_key", "model_parameters"}
        }

        # OpenRouter uses an OpenAI-compatible API at a custom base URL
        if is_openrouter and "base_url" not in provider_params:
            provider_params["base_url"] = "https://openrouter.ai/api/v1"

        # Map api_base to base_url (api_base is a legacy LiteLLM convention)
        if "api_base" in provider_params:
            provider_params["base_url"] = provider_params.pop("api_base")

        # Get model_parameters from llm_parameters config
        model_parameters = config.get("model_parameters", {})

        # Import the provider class dynamically
        client_class_path = cls._provider_classes[provider]
        module_path, class_name = client_class_path.rsplit(".", 1)

        # Import the module and get the class
        module = importlib.import_module(module_path)
        client_class = getattr(module, class_name)

        logger.info(f"Creating {class_name} for model: {model}")

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

        # OpenRouter models always use the OpenAI-compatible API
        if model.startswith("openrouter/"):
            return "openai"

        raise ValueError(
            f"Cannot determine provider for model: '{model}'. "
            f"Please specify a 'provider' explicitly in the config. "
            f"Known prefixes: {list(cls._provider_prefixes.keys())}. "
            f"Known providers: {list(cls._provider_classes.keys())}."
        )

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
