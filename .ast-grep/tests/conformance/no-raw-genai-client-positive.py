"""Fixture: google.genai clients built with the raw constructor must be flagged."""

from google import genai


def build_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


def build_live_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})
