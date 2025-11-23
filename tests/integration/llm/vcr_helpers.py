"""VCR helper functions for LLM integration tests."""

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def normalize_llm_request_body(body: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize LLM request body for consistent matching.

    This handles:
    - Sorting keys for consistent ordering
    - Normalizing dynamic values like timestamps
    - Ensuring consistent formatting of nested structures
    """
    normalized = {}

    # Handle messages array
    if "messages" in body:
        normalized["messages"] = []
        for msg in body["messages"]:
            norm_msg = {"role": msg.get("role")}

            # Handle different content types
            content = msg.get("content")
            if isinstance(content, str):
                norm_msg["content"] = content
            elif isinstance(content, list):
                # For multipart content (text + images)
                norm_msg["content"] = sorted(content, key=lambda x: x.get("type", ""))
            else:
                norm_msg["content"] = content

            normalized["messages"].append(norm_msg)

    # Handle Google-style contents array (used by Gemini client)
    if "contents" in body:
        normalized["contents"] = body["contents"]

    # Handle model parameter
    if "model" in body:
        normalized["model"] = body["model"]

    # Handle tools array
    if "tools" in body:
        # Sort tools by function name for consistent ordering
        normalized["tools"] = sorted(
            body["tools"], key=lambda t: t.get("function", {}).get("name", "")
        )

    # Handle tool_choice
    if "tool_choice" in body:
        normalized["tool_choice"] = body["tool_choice"]

    # Handle streaming parameter
    if "stream" in body:
        normalized["stream"] = body["stream"]

    # Handle other parameters (temperature, max_tokens, etc.)
    for key in [
        "temperature",
        "max_tokens",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
    ]:
        if key in body:
            normalized[key] = body[key]

    return normalized


def llm_request_matcher(r1: Any, r2: Any) -> bool:  # noqa: ANN401 # VCR request objects are dynamically typed
    """
    Custom matcher for LLM API requests.

    Compares normalized request bodies to handle variations in:
    - Key ordering
    - Whitespace differences
    - Dynamic values
    """
    # First check basic attributes
    if r1.method != r2.method:
        return False
    if r1.host != r2.host:
        return False
    if r1.path != r2.path:
        return False

    # For POST requests, compare normalized bodies
    if r1.method == "POST":
        try:
            body1 = json.loads(r1.body) if isinstance(r1.body, str | bytes) else r1.body
            body2 = json.loads(r2.body) if isinstance(r2.body, str | bytes) else r2.body

            if isinstance(body1, dict) and isinstance(body2, dict):
                norm1 = normalize_llm_request_body(body1)
                norm2 = normalize_llm_request_body(body2)

                # Log for debugging
                if norm1 != norm2:
                    logger.debug(
                        f"Request bodies don't match:\nNorm1: {norm1}\nNorm2: {norm2}"
                    )

                return norm1 == norm2
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(f"Failed to parse request body for matching: {e}")
            # Fall back to exact body matching
            return r1.body == r2.body

    return True


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def sanitize_response(response: dict[str, Any]) -> dict[str, Any] | None:
    """
    Remove sensitive data from responses before recording.

    This function is used as a before_record_response callback.

    Returns None to skip recording responses with empty bodies (prevents duplicates
    when VCR intercepts multiple HTTP client libraries like httpx + aiohttp).
    """
    # Skip recording responses with empty bodies - these are typically duplicates
    # from VCR intercepting multiple HTTP client libraries (httpx + aiohttp)
    if (
        "body" in response
        and "string" in response["body"]
        and (
            isinstance(response["body"]["string"], bytes)
            and not response["body"]["string"]
        )
    ):
        logger.debug("Skipping empty response body (likely VCR duplicate interception)")
        return None

    # Create a copy to avoid modifying the original
    sanitized = response.copy()

    # Sanitize headers
    if "headers" in sanitized:
        headers = sanitized["headers"]
        sensitive_headers = [
            "x-api-key",
            "api-key",
            "authorization",
            "x-goog-api-key",
            "openai-api-key",
            "openai-organization",
        ]

        for header in sensitive_headers:
            if header in headers:
                headers[header] = ["REDACTED"]
            # Also check case variations
            header_lower = header.lower()
            for h in list(headers.keys()):
                if h.lower() == header_lower:
                    headers[h] = ["REDACTED"]

    # Sanitize response body if needed
    if "body" in sanitized and "string" in sanitized["body"]:
        try:
            # Parse JSON body
            body_str = sanitized["body"]["string"]

            # Skip if body is bytes (likely compressed)
            if isinstance(body_str, bytes):
                # Don't try to parse compressed content
                return sanitized

            body_data = json.loads(body_str) if body_str else {}

            # Remove any API keys or sensitive data from response
            # (Most LLM APIs don't return sensitive data, but check just in case)
            if isinstance(body_data, dict):
                # Add any response sanitization logic here if needed
                pass

        except (json.JSONDecodeError, KeyError):
            # Not JSON or parsing failed, leave as is
            pass

    return sanitized


def generate_cassette_name(test_name: str, provider: str, model: str) -> str:
    """
    Generate a descriptive cassette filename.

    Args:
        test_name: Name of the test function
        provider: LLM provider (openai, google, etc.)
        model: Model name

    Returns:
        Cassette filename
    """
    # Sanitize model name for filesystem
    safe_model = model.replace("/", "_").replace(":", "_")

    return f"{test_name}_{provider}_{safe_model}.yaml"


def hash_request_for_cache(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    messages: list[dict[str, Any]],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    tools: list[dict[str, Any]] | None = None,
    **kwargs: Any,  # noqa: ANN401 # Generic LLM parameters are dynamically typed
) -> str:
    """
    Generate a hash for an LLM request for caching purposes.

    This creates a deterministic hash that can be used to identify
    identical requests across test runs.
    """
    # Create normalized request structure
    request_data = {"messages": messages, "tools": tools or [], "params": kwargs}

    # Normalize the data
    normalized = normalize_llm_request_body(request_data)

    # Create hash
    request_str = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(request_str.encode()).hexdigest()[:16]
