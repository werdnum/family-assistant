"""Google-specific types for LLM integration.

These types encapsulate Google Gemini-specific data that must be preserved
exactly as received from the API.
"""

import base64
import logging
from typing import Any

from pydantic_core import core_schema

logger = logging.getLogger(__name__)


class GeminiThoughtSignature:
    """Opaque wrapper for Gemini thought signatures.

    Thought signatures are opaque data that must be passed back to Google
    exactly as received.

    According to Google's documentation:
    "As a general rule, if you receive a thought signature in a model response,
    you should pass it back exactly as received when sending the conversation
    history in the next turn."
    """

    def __init__(self, raw_value: bytes) -> None:
        """Initialize from raw thought signature bytes.

        Args:
            raw_value: Thought signature as bytes from Google SDK.
        """
        if not isinstance(raw_value, bytes):
            raise TypeError(f"Expected bytes, got {type(raw_value)}")
        self._opaque_bytes = raw_value

    def to_google_format(self) -> bytes:
        """Return thought signature for sending to Google SDK.

        Returns:
            Thought signature as bytes, exactly as received.
        """
        return self._opaque_bytes

    def to_storage_string(self) -> str:
        """Convert to string for JSON storage.

        This is the ONLY place where encoding happens.
        Uses base64 encoding to safely store arbitrary binary data.
        """
        return base64.b64encode(self._opaque_bytes).decode("ascii")

    @classmethod
    def from_storage_string(cls, value: str) -> "GeminiThoughtSignature":
        """Create from string loaded from JSON storage.

        This is the ONLY place where decoding happens.
        Expects base64-encoded string from storage.
        """
        decoded = base64.b64decode(value)
        return cls(decoded)

    def __repr__(self) -> str:
        """Return string representation."""
        return f"GeminiThoughtSignature(length={len(self._opaque_bytes)})"


class GeminiProviderMetadata:
    """Provider metadata for Google Gemini tool calls.

    Encapsulates Gemini-specific metadata that must be preserved across
    conversation turns.
    """

    def __init__(
        self,
        thought_signature: GeminiThoughtSignature | None = None,
        interaction_id: str | None = None,
    ) -> None:
        """Initialize provider metadata.

        Args:
            thought_signature: Optional thought signature for this tool call.
            interaction_id: Optional interaction ID for Deep Research sessions.
        """
        self.thought_signature = thought_signature
        self.interaction_id = interaction_id

    # ast-grep-ignore: no-dict-any - JSON serialization
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON storage.

        Returns:
            Dict with provider metadata.
        """
        # ast-grep-ignore: no-dict-any - JSON serialization
        result: dict[str, Any] = {"provider": "google"}
        if self.thought_signature:
            # Convert thought signature to string for storage
            result["thought_signature"] = self.thought_signature.to_storage_string()
        if self.interaction_id:
            result["interaction_id"] = self.interaction_id
        return result

    @classmethod
    # ast-grep-ignore: no-dict-any - JSON deserialization
    def from_dict(cls, data: dict[str, Any]) -> "GeminiProviderMetadata":
        """Deserialize from dict.

        Args:
            data: Dict containing provider metadata.

        Returns:
            GeminiProviderMetadata instance.
        """
        thought_sig = None
        if "thought_signature" in data:
            # Convert string from storage back to thought signature
            thought_sig = GeminiThoughtSignature.from_storage_string(
                data["thought_signature"]
            )
        interaction_id = data.get("interaction_id")
        return cls(thought_signature=thought_sig, interaction_id=interaction_id)

    def __repr__(self) -> str:
        """Return string representation."""
        return f"GeminiProviderMetadata(thought_signature={self.thought_signature}, interaction_id={self.interaction_id})"

    @classmethod
    def __get_pydantic_core_schema__(  # noqa: PLW3201
        cls,
        _source_type: Any,  # noqa: ANN401
        _handler: Any,  # noqa: ANN401
    ) -> core_schema.CoreSchema:
        """Define Pydantic serialization schema.

        This tells Pydantic how to serialize GeminiProviderMetadata objects
        when calling model_dump(mode='json').
        """
        return core_schema.no_info_plain_validator_function(
            cls._pydantic_validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                cls._pydantic_serialize,
                when_used="json",
            ),
        )

    @classmethod
    # ast-grep-ignore: no-dict-any - Pydantic validation
    def _pydantic_validate(cls, value: Any) -> "GeminiProviderMetadata":  # noqa: ANN401
        """Pydantic validation - accept existing instances or dicts."""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls.from_dict(value)
        raise ValueError(f"Cannot convert {type(value)} to GeminiProviderMetadata")

    # ast-grep-ignore: no-dict-any - Pydantic serialization
    def _pydantic_serialize(self) -> dict[str, Any]:
        """Pydantic serialization - convert to dict for JSON."""
        return self.to_dict()
