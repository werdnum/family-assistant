"""Integration tests for HomeAssistantClientWrapper with real Home Assistant."""

import aiohttp
import homeassistant_api
import pytest

from family_assistant.plugins.home_assistant.client import HomeAssistantClientWrapper
from tests.integration.home_assistant.bulk_entities import BULK_ENTITY_ID_PREFIX


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


BULK_ENTITY_COUNT = 1200


@pytest.mark.integration
# The chunk renders share a URL, so each recorded response is replayed once, in order.
@pytest.mark.vcr(allow_playback_repeats=False)
async def test_entity_list_on_an_install_too_large_for_one_template(
    home_assistant_service: tuple[str, str | None],
) -> None:
    """Every entity is listed when one render of them all passes HA's output limit.

    The old single-template listing failed here with HA's "template is invalid".
    """
    base_url, token = home_assistant_service
    bulk = {
        f"{BULK_ENTITY_ID_PREFIX}{n:04d}": f"Upstairs bedroom climate sensor number {n:04d} temperature"
        for n in range(BULK_ENTITY_COUNT)
    }
    headers = {"Authorization": f"Bearer {token}"}
    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api", token=token or "test", use_async=True
    )
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url, token=token or "test", client=ha_lib_client
    )
    # Recording runs against a live HA, which needs the entities created; a
    # replay has no token and reads the recorded renders instead.
    async with aiohttp.ClientSession(headers=headers) as setup:
        try:
            if token is not None:
                for entity_id, name in bulk.items():
                    async with setup.post(
                        f"{base_url}/api/states/{entity_id}",
                        json={"state": "21.5", "attributes": {"friendly_name": name}},
                    ) as response:
                        response.raise_for_status()

            listed = await wrapper.async_get_entity_list_with_metadata()
        finally:
            if token is not None:
                for entity_id in bulk:
                    async with setup.delete(f"{base_url}/api/states/{entity_id}"):
                        pass
            await ha_lib_client.async_cache_session.close()

    names = {entity["entity_id"]: entity["name"] for entity in listed}
    assert {entity_id: names.get(entity_id) for entity_id in bulk} == bulk
    assert len(names) == len(listed)
    assert "input_boolean.test_switch" in names
