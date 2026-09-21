"""Which HTTP responses are worth trying again.

408, 425 and 429 sit in the client-error range but are conditions of the
moment, so treating the whole 4xx range as settled would give up on a delivery
that a retry would have completed.
"""

from __future__ import annotations

import pytest

from family_assistant.utils.http_status import (
    RETRYABLE_4XX_STATUSES,
    is_transient_http_status,
)


@pytest.mark.parametrize("status_code", sorted(RETRYABLE_4XX_STATUSES))
def test_the_retryable_client_statuses_are_transient(status_code: int) -> None:
    assert is_transient_http_status(status_code) is True


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 413, 422])
def test_other_client_statuses_are_permanent(status_code: int) -> None:
    assert is_transient_http_status(status_code) is False


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_server_statuses_are_transient(status_code: int) -> None:
    assert is_transient_http_status(status_code) is True


def test_success_is_not_reported_as_transient() -> None:
    assert is_transient_http_status(200) is False
