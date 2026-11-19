"""Fixture: google.genai clients built through the project factory must not be flagged."""

from google import genai

from family_assistant.llm.genai_sdk import gemini_http_options, new_genai_client


def build_client(api_key: str) -> genai.Client:
    return new_genai_client(api_key=api_key)


def build_live_client(api_key: str) -> genai.Client:
    return new_genai_client(
        api_key=api_key, http_options=gemini_http_options(api_version="v1alpha")
    )
