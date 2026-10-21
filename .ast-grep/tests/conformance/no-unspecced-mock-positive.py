"""Fixture: unspecced mocks and patches that substitute a bare MagicMock must be flagged."""

import unittest.mock
from unittest import mock
from unittest.mock import (
    AsyncMock,
    MagicMock,
    Mock,
    NonCallableMagicMock,
    PropertyMock,
    ThreadingMock,
    create_autospec,
    patch,
)

from pytest_mock import MockerFixture


class Client:
    def fetch(self) -> str:
        return "real"


class FakeClient:
    def fetch(self) -> str:
        return "fake"


def bare_constructions() -> None:
    MagicMock()
    Mock(return_value=1)
    AsyncMock(side_effect=RuntimeError)
    NonCallableMagicMock()
    PropertyMock(return_value=3)
    ThreadingMock()
    mock.MagicMock()
    unittest.mock.AsyncMock()


def spec_without_spec_set() -> None:
    MagicMock(spec=Client)
    MagicMock(spec_set=Client)
    create_autospec(Client)
    create_autospec(Client, instance=True)
    create_autospec(Client, spec_set=False)


def patches_without_spec() -> None:
    with patch("module.Client"):
        pass
    with patch("module.Client", return_value=1):
        pass
    with patch.object(Client, "fetch"):
        pass
    with patch.object(Client, "fetch", return_value="x"):
        pass
    with mock.patch("module.Client", autospec=False):
        pass
    with patch.multiple("module", Client=FakeClient):
        pass


class UsesMocker:
    def __init__(self, mocker: MockerFixture) -> None:
        self.mocker = mocker

    def unspecced(self) -> None:
        self.mocker.patch("module.Client", return_value=1)
        self.mocker.MagicMock()
        self.mocker.stub()


def pytest_mock_fixture(mocker: MockerFixture) -> None:
    mocker.patch("module.Client")
    mocker.patch.object(Client, "fetch")
    mocker.AsyncMock()
    mocker.create_autospec(Client)
    mocker.async_stub()
    mocker.patch.multiple("module", Client=1)
    mocker.patch.context_manager(Client, "fetch")


@patch("module.Client")
def decorated(client: object) -> None:
    del client


def aliased_imports() -> None:
    import unittest.mock as um  # noqa: PLC0415
    from unittest import mock as mocker  # noqa: PLC0415
    from unittest.mock import MagicMock as Magic  # noqa: PLC0415
    from unittest.mock import patch as p  # noqa: PLC0415

    del um, mocker, Magic, p
