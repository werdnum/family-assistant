# ruff: noqa
# pyright: reportMissingImports=false, reportUndefinedVariable=false
"""Negative test cases for async-magic-mock hint rule.

These examples SHOULD NOT trigger the hint.
"""

from unittest.mock import MagicMock, Mock, AsyncMock


# Should NOT trigger: AsyncMock is correct for async functions
async def test_with_async_mock():
    mock_service = AsyncMock()


# Should NOT trigger: MagicMock in regular (non-async) function
def test_sync_with_magic_mock():
    mock_service = MagicMock()


# Should NOT trigger: Mock in regular function
def test_sync_with_mock():
    mock_obj = Mock()


# Should NOT trigger: AsyncMock with arguments
async def test_async_mock_with_args():
    mock_service = AsyncMock(return_value=42)
