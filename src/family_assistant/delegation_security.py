from __future__ import annotations

from enum import StrEnum


class DelegationSecurityLevel(StrEnum):
    """Policy controlling whether profile-to-profile delegation is allowed."""

    BLOCKED = "blocked"
    CONFIRM = "confirm"
    UNRESTRICTED = "unrestricted"
    NONE = "none"  # Legacy alias used in older test fixtures/configs.
