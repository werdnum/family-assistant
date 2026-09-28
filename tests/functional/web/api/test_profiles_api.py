"""Functional contract test for the profile listing API."""

from fastapi import FastAPI
from httpx import AsyncClient

from family_assistant.processing import ProcessingService


async def test_get_profiles_returns_selectable_default(
    api_test_client: AsyncClient,
    app_fixture: FastAPI,
    api_test_processing_service: ProcessingService,
) -> None:
    profile_id = api_test_processing_service.service_config.id
    app_fixture.state.processing_services = {profile_id: api_test_processing_service}

    response = await api_test_client.get("/api/v1/profiles")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["default_profile_id"] == profile_id
    assert [profile["id"] for profile in body["profiles"]] == [profile_id]
