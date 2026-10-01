"""A small async client for Trino's HTTP statement protocol."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:
    from family_assistant.plugins.trino.config import TrinoConfig

logger = logging.getLogger(__name__)


class TrinoQueryError(Exception):
    """Trino rejected or failed a statement; ``message`` is safe to show."""

    def __init__(self, message: str, *, error_name: str | None = None) -> None:
        super().__init__(message)
        self.error_name = error_name


@dataclass(frozen=True, slots=True)
class QueryResult:
    """The rows a statement returned, cut off at the client's row limit."""

    columns: list[str]
    # ast-grep-ignore: no-dict-any - Trino rows hold arbitrary JSON values
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class IoPlan:
    """What ``EXPLAIN (TYPE IO)`` says a statement reads and writes."""

    input_tables: frozenset[str]
    output_table: str | None


class TrinoClient:
    """Runs statements against one coordinator as one user."""

    def __init__(
        self,
        config: TrinoConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if config.url is None or config.user is None:
            raise ValueError("TrinoClient needs a url and a user")
        headers = {"X-Trino-User": config.user, "X-Trino-Source": config.source}
        if config.catalog is not None:
            headers["X-Trino-Catalog"] = config.catalog
        if config.schema_name is not None:
            headers["X-Trino-Schema"] = config.schema_name
        auth = (
            httpx.BasicAuth(config.user, config.password.get_secret_value())
            if config.password is not None
            else None
        )
        self._max_rows = config.max_rows
        self._client = httpx.AsyncClient(
            base_url=config.url.rstrip("/"),
            headers=headers,
            auth=auth,
            timeout=config.request_timeout_seconds,
            transport=transport,
        )

    async def io_plan(self, sql: str) -> IoPlan:
        """The tables ``sql`` would read, with views expanded to what they read."""
        result = await self.execute(f"EXPLAIN (TYPE IO, FORMAT JSON) {sql}")
        if len(result.rows) != 1 or len(result.rows[0]) != 1:
            raise TrinoQueryError("EXPLAIN (TYPE IO) returned an unexpected shape")
        return parse_io_plan(result.rows[0][0])

    async def execute(self, sql: str) -> QueryResult:
        """Run ``sql`` and collect its rows, up to the row limit."""
        response = await self._client.post("/v1/statement", content=sql)
        columns: list[str] | None = None
        rows: list[list[Any]] = []
        while True:
            payload = _payload(response)
            if (error := payload.get("error")) is not None:
                raise TrinoQueryError(
                    str(error.get("message") or "Trino query failed"),
                    error_name=error.get("errorName"),
                )
            if columns is None and "columns" in payload:
                columns = [str(column["name"]) for column in payload["columns"]]
            rows.extend(payload.get("data") or [])
            next_uri = payload.get("nextUri")
            if len(rows) > self._max_rows:
                if next_uri is not None:
                    await self._cancel(next_uri)
                return QueryResult(
                    columns=columns or [], rows=rows[: self._max_rows], truncated=True
                )
            if next_uri is None:
                return QueryResult(columns=columns or [], rows=rows)
            response = await self._client.get(next_uri)

    async def _cancel(self, next_uri: str) -> None:
        try:
            response = await self._client.delete(next_uri)
        except httpx.HTTPError:
            logger.warning("Could not cancel a truncated Trino query", exc_info=True)
            return
        if response.is_error:
            logger.warning(
                "Could not cancel a truncated Trino query: HTTP %s",
                response.status_code,
            )

    async def close(self) -> None:
        await self._client.aclose()


# ast-grep-ignore: no-dict-any - Trino protocol payloads are arbitrary JSON
def _payload(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise TrinoQueryError(
            f"Trino returned HTTP {response.status_code}: {response.text[:500]}"
        )
    return response.json()


def parse_io_plan(raw: object) -> IoPlan:
    """Read the JSON ``EXPLAIN (TYPE IO, FORMAT JSON)`` produces."""
    try:
        plan = cast("dict[str, Any]", json.loads(raw) if isinstance(raw, str) else raw)
        inputs = frozenset(
            _table_name(info["table"]) for info in plan["inputTableColumnInfos"]
        )
        output = plan.get("outputTable")
        return IoPlan(
            input_tables=inputs,
            output_table=_table_name(output) if output is not None else None,
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise TrinoQueryError(f"Could not read the IO plan: {exc}") from exc


# ast-grep-ignore: no-dict-any - Trino protocol payloads are arbitrary JSON
def _table_name(table: dict[str, Any]) -> str:
    schema_table = table["schemaTable"]
    return f"{table['catalog']}.{schema_table['schema']}.{schema_table['table']}"
