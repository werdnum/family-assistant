import httpx
import pytest


@pytest.mark.asyncio
async def test_version_api(api_client: httpx.AsyncClient) -> None:
    """Test the version API endpoint."""
    resp = await api_client.get("/api/version")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert "git_commit" in data
    assert "build_date" in data
    assert data["version"] == "0.1.0"
    # In test environment, these should be "unknown" unless set
    assert data["git_commit"] == "unknown"
    assert data["build_date"] == "unknown"
