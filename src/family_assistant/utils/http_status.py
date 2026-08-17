"""Which HTTP responses are worth trying again."""

from __future__ import annotations

RETRYABLE_4XX_STATUSES = frozenset({408, 425, 429})
"""Client-range statuses that an identical request can still succeed after.

Request timeout, too early, and rate limited. The rest of the 4xx range says
the request itself is wrong, and repeating it verbatim will be refused again.
"""


def is_transient_http_status(status_code: int) -> bool:
    """Whether repeating the identical request could succeed later."""
    if 400 <= status_code < 500:
        return status_code in RETRYABLE_4XX_STATUSES
    return status_code >= 500
