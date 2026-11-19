"""The one place a ``google.genai`` SDK client is constructed.

google-genai 3 changed the Gemini Developer API default from ``v1beta`` to
``v1``. Models, request fields and the ``-latest`` aliases this application
uses exist only on ``v1beta``, so a client built with the SDK's defaults sends
every call to a version that rejects some of them. Every client is built here,
pinned to ``v1beta`` unless the caller names another version (Gemini Live uses
``v1alpha``). The ``no-raw-genai-client`` conformance rule keeps it that way.

``google.genai`` is imported inside the functions: several callers import it
lazily, and importing this module must not undo that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google import genai
    from google.genai import types

GEMINI_API_VERSION = "v1beta"


def gemini_http_options(
    *, base_url: str | None = None, api_version: str = GEMINI_API_VERSION
) -> types.HttpOptions:
    """SDK HTTP options for the Gemini Developer API."""
    from google.genai import types  # noqa: PLC0415 - keep google.genai lazy

    return types.HttpOptions(base_url=base_url, api_version=api_version)


def new_genai_client(
    *,
    api_key: str | None,
    http_options: types.HttpOptions | None = None,
    debug_config: types.DebugConfig | None = None,
) -> genai.Client:
    """A Gemini Developer API client, on ``v1beta`` unless *http_options* says otherwise.

    *http_options* without an ``api_version`` gets ``v1beta``; one that sets
    a version keeps it.
    """
    from google import genai  # noqa: PLC0415 - keep google.genai lazy

    options = http_options or gemini_http_options()
    if options.api_version is None:
        options = options.model_copy(update={"api_version": GEMINI_API_VERSION})
    if debug_config is None:
        return genai.Client(api_key=api_key, http_options=options)
    return genai.Client(
        api_key=api_key, http_options=options, debug_config=debug_config
    )
