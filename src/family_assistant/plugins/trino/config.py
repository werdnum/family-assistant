"""Configuration for one Trino coordinator."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from family_assistant.security.taint import SourceTrustTier

if TYPE_CHECKING:
    from collections.abc import Iterable


class TrinoConfig(BaseModel):
    """Where Trino is, who to query it as, and how trustworthy each table is."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    # Optional here because deployments usually supply them through the
    # TRINO_URL, TRINO_USER and TRINO_PASSWORD environment variables, which are
    # applied after the YAML is first validated. An instance without a URL or
    # user is not started.
    url: str | None = None
    user: str | None = None
    # HTTP Basic password; omit it for a coordinator that trusts the user name.
    password: SecretStr | None = None
    catalog: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    source: str = "family-assistant"
    request_timeout_seconds: float = Field(default=60, gt=0)
    max_rows: int = Field(default=200, gt=0)
    # Glob patterns over lower-case "catalog.schema.table" names, each mapped to
    # the trust tier of what that table holds. A table takes the least trusted
    # tier of every pattern it matches; a table no pattern matches is
    # unknown_external, so an empty map grades every result as untrusted.
    table_tiers: dict[str, SourceTrustTier] = Field(default_factory=dict)

    @field_validator("table_tiers", mode="before")
    @classmethod
    def _parse_tiers(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            str(pattern).lower(): SourceTrustTier.from_value(tier)
            for pattern, tier in value.items()
        }

    def tier_for_tables(self, tables: Iterable[str]) -> SourceTrustTier | None:
        """The least trusted tier among ``tables``, or ``None`` for no tables."""
        tiers = [self._tier_for_table(table.lower()) for table in tables]
        return max(tiers) if tiers else None

    def _tier_for_table(self, table: str) -> SourceTrustTier:
        matches = [
            tier
            for pattern, tier in self.table_tiers.items()
            if fnmatchcase(table, pattern)
        ]
        return max(matches) if matches else SourceTrustTier.UNKNOWN_EXTERNAL
