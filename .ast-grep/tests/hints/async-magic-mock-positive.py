# ruff: noqa
# pyright: reportMissingImports=false, reportUndefinedVariable=false
"""Positive test cases for async-magic-mock hint rule.

These examples SHOULD trigger the hint.
"""

from unittest.mock import MagicMock, Mock


# Should trigger: MagicMock in async function
async def test_with_magic_mock():
    mock_service = MagicMock()


# Should trigger: Mock in async function
async def test_with_mock():
    mock_obj = Mock()


# Should trigger: MagicMock with arguments
async def test_magic_mock_with_args():
    mock_service = MagicMock(return_value=42)


# Should trigger: Multiple mocks
async def test_multiple_mocks():
    mock1 = MagicMock()
    mock2 = Mock()
