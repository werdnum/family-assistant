"""Fixture: google.genai clients built with the raw constructor must be flagged."""

import google.genai as aliased_genai
from google import genai
from google import genai as google_genai
from google.genai import Client
from google.genai.client import Client as SdkClient


def build_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


def build_live_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})


def build_aliased_clients(api_key: str) -> list[genai.Client]:
    return [
        Client(api_key=api_key),
        SdkClient(api_key=api_key),
        google_genai.Client(api_key=api_key),
        aliased_genai.Client(api_key=api_key),
    ]
