"""Fixture: specced mocks, explicit replacements and real fakes must not be flagged."""

import os
import unittest.mock
from unittest import mock
from unittest.mock import create_autospec, patch

from pytest_mock import MockerFixture


class Client:
    def fetch(self) -> str:
        return "real"


class FakeClient:
    def fetch(self) -> str:
        return "fake"


def unrelated_mock_alias() -> None:
    from some_package import mock as helper  # noqa: PLC0415

    del helper, unittest.mock


def specced() -> None:
    create_autospec(Client, spec_set=True)
    create_autospec(Client, instance=True, spec_set=True)
    mock.create_autospec(Client, spec_set=True, instance=True)


def explicit_replacements() -> None:
    with patch("module.Client", autospec=True):
        pass
    with patch("module.Client", FakeClient):
        pass
    with patch("module.Client", new=FakeClient):
        pass
    with patch.object(Client, "fetch", autospec=True):
        pass
    with patch.object(Client, "fetch", FakeClient.fetch):
        pass
    with patch.object(Client, "fetch", new=FakeClient.fetch):
        pass
    with patch.dict(os.environ, {"KEY": "value"}):
        pass


def pytest_mock_specced(mocker: MockerFixture) -> None:
    mocker.patch("module.Client", autospec=True)
    mocker.patch.object(Client, "fetch", new=FakeClient.fetch)
    mocker.create_autospec(Client, spec_set=True)


@patch("module.Client", autospec=True)
def decorated(client: object) -> None:
    del client
