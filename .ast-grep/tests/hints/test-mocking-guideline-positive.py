# ruff: noqa
# pyright: reportMissingImports=false, reportUndefinedVariable=false
# pylint: skip-file
"""Positive test cases for test-mocking-guideline hint rule.

These examples SHOULD trigger the hint.
"""

from unittest.mock import MagicMock, Mock, AsyncMock


# Should trigger: MagicMock in test
def test_with_magic_mock():
    mock_service = MagicMock()


# Should trigger: Mock in test
def test_with_mock():
    mock_obj = Mock()


# Should trigger: AsyncMock in test
async def test_with_async_mock():
    mock_service = AsyncMock()


# Should trigger: Mock with spec
def test_with_mock_spec():
    mock_service = MagicMock(spec=SomeClass)


# Should trigger: Multiple mocks
def test_multiple_mocks():
    mock1 = MagicMock()
    mock2 = AsyncMock()
