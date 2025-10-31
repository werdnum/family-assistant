"""Integration tests for HomeAssistantClientWrapper with real Home Assistant."""

import aiohttp
import homeassistant_api
import pytest

from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper


@pytest.mark.integration
@pytest.mark.vcr
async def test_camera_snapshot_retrieval(
    home_assistant_service: tuple[str, str | None],
) -> None:
    """Test retrieving camera snapshot through our wrapper with real HA."""
    base_url, token = home_assistant_service

    # Create library client
    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )

    # Create our wrapper
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    try:
        # Test our custom camera snapshot method
        # The demo camera platform provides camera.demo_camera
        image_data = await wrapper.async_get_camera_snapshot("camera.demo_camera")

        # Verify we got binary data
        assert isinstance(image_data, bytes)
        assert len(image_data) > 0

        # Verify it looks like an image (check for common image magic bytes)
        # JPEG: 0xFF 0xD8, PNG: 0x89 0x50, GIF: 'GIF'
        assert image_data[:2] in {b"\xff\xd8", b"\x89P"} or image_data[:3] == b"GIF"

    finally:
        await ha_lib_client.async_cache_session.close()


@pytest.mark.integration
@pytest.mark.vcr
async def test_camera_snapshot_nonexistent_camera(
    home_assistant_service: tuple[str, str | None],
) -> None:
    """Test error handling when requesting snapshot from non-existent camera."""
    base_url, token = home_assistant_service

    # Create library client
    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )

    # Create our wrapper
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    try:
        # Try to get snapshot from non-existent camera
        # This should raise an HTTP 404 error
        with pytest.raises(aiohttp.ClientResponseError):
            await wrapper.async_get_camera_snapshot("camera.nonexistent_camera")

    finally:
        await ha_lib_client.async_cache_session.close()
