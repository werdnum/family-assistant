"""Test file to trigger hints."""

from unittest.mock import AsyncMock, MagicMock


async def test_with_magic_mock() -> None:
    """This should trigger the async-magic-mock hint."""
    mock_service = MagicMock()
    assert mock_service is not None


async def test_with_async_mock() -> None:
    """This should trigger the test-mocking-guideline hint."""
    mock_service = AsyncMock()
    assert mock_service is not None
