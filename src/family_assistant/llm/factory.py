"""
Factory for creating appropriate LLM clients based on model configuration.
"""

import importlib
import logging
import os
from typing import TYPE_CHECKING, Any

from family_assistant.llm.custom_instructions import (
    CustomInstructionsClient,
    split_custom_instructions,
)

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.connections import ProviderConnections

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
        connections: "ProviderConnections | None" = None,
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
            connections: Registry the clients borrow their provider SDK clients
                from, so clients on one endpoint share a connection pool. Each
                client builds a private one when omitted.

        Returns:
            Instantiated LLM client

        Raises:
            ValueError: If model/provider is not recognized
        """
        # Check for retry configuration
        if "retry_config" in config:
            retry_config = config["retry_config"]

            # Create primary client
            primary_client = cls._create_single_client(
                retry_config["primary"], connections
            )
            primary_model = retry_config["primary"]["model"]

            # Create fallback if specified
            fallback_client = None
            fallback_model = None
            if "fallback" in retry_config:
                fallback_client = cls._create_single_client(
                    retry_config["fallback"], connections
                )
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
        return cls._create_single_client(config, connections)

    @classmethod
    def _create_single_client(
        cls,
        # ast-grep-ignore: no-dict-any - LLM config dict has varying keys per provider/format
        config: dict[str, Any],
        connections: "ProviderConnections | None",
    ) -> "LLMInterface":
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
            if k not in {"model", "provider", "api_key", "model_parameters", "flex"}
        }

        # OpenRouter uses an OpenAI-compatible API at a custom base URL
        if is_openrouter and "base_url" not in provider_params:
            provider_params["base_url"] = "https://openrouter.ai/api/v1"

        # Map api_base to base_url (api_base is a legacy LiteLLM convention)
        if "api_base" in provider_params:
            provider_params["base_url"] = provider_params.pop("api_base")

        # Custom instructions ride in the llm_parameters map but are not a
        # request parameter, so the provider client never sees them.
        custom_instructions, model_parameters = split_custom_instructions(
            model, config.get("model_parameters") or {}
        )
        flex = bool(config.get("flex"))
        if flex:
            cls._validate_flex(provider, model, model_parameters, provider_params)

        # Import the provider class dynamically
        client_class_path = cls._provider_classes[provider]
        module_path, class_name = client_class_path.rsplit(".", 1)

        # Import the module and get the class
        module = importlib.import_module(module_path)
        client_class = getattr(module, class_name)

        logger.info(f"Creating {class_name} for model: {model}")

        client = client_class(
            api_key=api_key,
            model=model,
            model_parameters=model_parameters,
            connections=connections,
            **provider_params,
        )
        if custom_instructions:
            client = CustomInstructionsClient(client, instructions=custom_instructions)
        if not flex:
            return client
        from .flex import FlexClient  # noqa: PLC0415 - flex imports this package

        return FlexClient(client, model=model)

    @classmethod
    def validate_flex_config(
        cls,
        # ast-grep-ignore: no-dict-any - LLM config dict has varying keys per provider/format
        config: dict[str, Any],
    ) -> None:
        """Refuse a single-client config whose ``flex`` it could not honour.

        The same check the factory runs when it builds the client, without
        building it, so configuration nothing builds yet still fails at startup.
        """
        if not config.get("flex"):
            return
        model = config.get("model")
        if not model:
            raise ValueError("Model must be specified in config")
        provider = config.get("provider") or cls._determine_provider(model)
        provider_params = {
            k: v
            for k, v in config.items()
            if k not in {"model", "provider", "api_key", "model_parameters", "flex"}
        }
        cls._validate_flex(
            provider, model, config.get("model_parameters", {}), provider_params
        )

    @staticmethod
    def _validate_flex(
        provider: str,
        model: str,
        model_parameters: dict[str, dict[str, object]],
        # ast-grep-ignore: no-dict-any - Provider kwargs have varying types.
        provider_params: dict[str, Any],
    ) -> None:
        from .flex import FLEX_PROVIDERS  # noqa: PLC0415 - flex imports this package
        from .providers.google_genai_client import (  # noqa: PLC0415 - provider modules load on demand
            is_interactions_agent_model,
        )

        if provider not in FLEX_PROVIDERS:
            raise ValueError(
                f"Model '{model}' sets flex, but provider '{provider}' has no Flex "
                f"price class. Flex is available on: {', '.join(sorted(FLEX_PROVIDERS))}."
            )
        if is_interactions_agent_model(model):
            raise ValueError(
                f"Model '{model}' sets flex, but it is an Interactions API agent, "
                "which takes no price class."
            )
        configured = {
            pattern
            for pattern, params in model_parameters.items()
            if pattern in model and "service_tier" in params
        }
        if configured or "service_tier" in provider_params:
            raise ValueError(
                f"Model '{model}' sets flex and also configures service_tier "
                f"({', '.join(sorted(configured)) or 'directly'}). Flex chooses the "
                "price class per call; remove the explicit service_tier."
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
