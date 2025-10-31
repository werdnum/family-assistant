"""VCR.py compatibility patches for Home Assistant integration tests.

Addresses VCR.py issue #927 - MockClientResponse.content property
lacks a setter, breaking compatibility with aiohttp 3.12+ and homeassistant_api.

See also: tests/integration/llm/streaming_mocks.py for similar workaround.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from vcr.stubs import aiohttp_stubs

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(scope="session", autouse=True)
def patch_vcr_mock_client_response() -> Generator[None]:
    """Patch VCR's MockClientResponse to add content setter for aiohttp 3.12+ compatibility.

    VCR's MockClientResponse defines `content` as a read-only @property, but aiohttp 3.12+
    and/or homeassistant_api library attempt to assign to response.content during request
    processing. This causes AttributeError: "property 'content' of 'MockClientResponse'
    object has no setter".

    This fixture monkey-patches the property to add a setter that silently accepts
    the assignment without actually storing it (VCR manages content via _body internally).

    The patch is automatically applied to all tests in this directory via autouse=True.
    """
    # Store reference to original property
    original_property = aiohttp_stubs.MockClientResponse.content

    # Create new property with both getter and setter
    # ruff: noqa: ANN401 - Any is appropriate for monkey-patching VCR internals
    def content_getter(self: Any) -> Any:  # noqa: ANN401
        """Delegate to original getter."""
        if original_property.fget is None:
            raise RuntimeError("Original content property has no getter")
        return original_property.fget(self)

    def content_setter(self: Any, value: Any) -> None:  # noqa: ANN401
        """Allow setting for compatibility, but ignore the value.

        VCR manages content via _body internally and reconstructs it
        on access via the getter. We don't need to store the value.
        """
        pass

    # Replace the property with one that has both getter and setter
    # Type ignore: monkey-patching class attribute is expected here
    aiohttp_stubs.MockClientResponse.content = property(  # type: ignore[misc]
        fget=content_getter,
        fset=content_setter,
    )

    yield

    # Restore original property (cleanup)
    # Type ignore: monkey-patching class attribute is expected here
    aiohttp_stubs.MockClientResponse.content = original_property  # type: ignore[misc]
