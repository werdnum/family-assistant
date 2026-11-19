"""Tests for the google.genai client factory."""

from google import genai

from family_assistant.llm.genai_sdk import gemini_http_options, new_genai_client


def _api_version(client: genai.Client) -> str | None:
    return client._api_client._http_options.api_version


def test_a_client_defaults_to_v1beta() -> None:
    client = new_genai_client(api_key="test-key")

    assert _api_version(client) == "v1beta"


def test_options_without_a_version_get_v1beta() -> None:
    client = new_genai_client(
        api_key="test-key",
        http_options=gemini_http_options(base_url="https://proxy.example/"),
    )

    assert _api_version(client) == "v1beta"


def test_an_explicit_version_is_kept() -> None:
    client = new_genai_client(
        api_key="test-key", http_options=gemini_http_options(api_version="v1alpha")
    )

    assert _api_version(client) == "v1alpha"
