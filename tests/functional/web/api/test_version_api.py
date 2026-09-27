import httpx
import pytest

from family_assistant import __version__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("git_commit", "build_date"),
    [
        ("abc123", "2026-01-01T00:00:00Z"),
        (None, None),
    ],
)
async def test_version_api(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    git_commit: str | None,
    build_date: str | None,
) -> None:
    """Report the package version and the configured build provenance."""
    if git_commit is None:
        monkeypatch.delenv("GIT_COMMIT", raising=False)
        monkeypatch.delenv("BUILD_DATE", raising=False)
    else:
        monkeypatch.setenv("GIT_COMMIT", git_commit)
        assert build_date is not None
        monkeypatch.setenv("BUILD_DATE", build_date)

    resp = await api_client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json() == {
        "version": __version__,
        "git_commit": git_commit or "unknown",
        "build_date": build_date or "unknown",
    }
