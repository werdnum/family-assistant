"""
Defines the schema for vector search queries and implements the query logic.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.sql import text  # For executing raw SQL if needed

from .database import DatabaseExecutor

logger = logging.getLogger(__name__)

# --- Input Schema Definition ---


@dataclass(frozen=True)  # Use frozen=True for immutability if desired
class MetadataFilter:
    """Represents a simple key-value filter for JSONB metadata."""

    key: str
    value: str  # Keep value as string for simplicity, conversion happens in query logic


@dataclass
class VectorSearchQuery:
    """
    Input schema for performing vector/keyword/hybrid searches.
    """

    search_type: Literal["semantic", "keyword", "hybrid"] = "hybrid"

    # Query Content
    semantic_query: str | None = None
    keywords: str | None = None
    # Note: query_embedding is generated based on semantic_query and embedding_model,
    # so it's not part of the raw input schema but passed separately to the query function.

    # Model Selection (Required for semantic/hybrid)
    embedding_model: str | None = None

    # Filters
    embedding_types: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    excluded_source_types: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    created_after: datetime | None = None  # Expect timezone-aware datetime
    created_before: datetime | None = None  # Expect timezone-aware datetime
    title_like: str | None = None
    metadata_filters: list[MetadataFilter] = field(
        default_factory=list
    )  # Changed to list
    visibility_grants: set[str] | None = None

    # Control Parameters
    limit: int = 10
    rrf_k: int = 60  # Constant for Reciprocal Rank Fusion

    def __post_init__(self) -> None:
        """Basic validation."""
        if self.search_type in {"semantic", "hybrid"} and not self.semantic_query:
            raise ValueError(
                "semantic_query is required for 'semantic' or 'hybrid' search."
            )
        if self.search_type in {"semantic", "hybrid"} and not self.embedding_model:
            raise ValueError(
                "embedding_model is required for 'semantic' or 'hybrid' search."
            )
        if self.search_type == "keyword" and not self.keywords:
            raise ValueError("keywords are required for 'keyword' search.")
        if self.limit <= 0:
            raise ValueError("limit must be positive.")
        if self.rrf_k <= 0:
            raise ValueError("rrf_k must be positive.")


# --- Query Function ---


def _build_in_clause_for_sqlite(
    params: dict[str, object],
    where_clauses: list[str],
    column_name: str,
    values: list[str],
    param_prefix: str,
) -> None:
    """
    Helper function to build IN clause for SQLite compatibility.

    Args:
        params: Dictionary to add parameters to
        where_clauses: List to append the WHERE clause to
        column_name: The column to filter on (e.g., "d.source_type")
        values: List of values for the IN clause
        param_prefix: Prefix for parameter names (e.g., "doc_source_type")
    """
    placeholders = ", ".join(f":{param_prefix}_{i}" for i in range(len(values)))
    for i, value in enumerate(values):
        params[f"{param_prefix}_{i}"] = value
    where_clauses.append(f"{column_name} IN ({placeholders})")


@dataclass
class _SearchParts:
    """Composable SQL fragments for vector, keyword, and hybrid search."""

    vector_cte: str = ""
    fts_cte: str = ""
    select_columns: list[str] = field(
        default_factory=lambda: [
            "de.id AS embedding_id",
            "de.document_id",
            "d.title",
            "d.source_type",
            "d.created_at",
            "de.embedding_type",
            "de.content AS embedding_source_content",
            "d.source_id",
            "d.source_uri",
            "d.doc_metadata",
            "d.file_path",
            "de.chunk_index",
        ]
    )
    joins: list[str] = field(default_factory=list)
    result_predicates: list[str] = field(default_factory=list)
    order_by: str = ""


def _validate_query_embedding(
    query: VectorSearchQuery, query_embedding: list[float] | None
) -> None:
    if query.search_type in {"semantic", "hybrid"} and not query_embedding:
        raise ValueError(
            "query_embedding is required for semantic/hybrid search execution."
        )


def _sqlite_search_is_unsupported(
    db_context: DatabaseExecutor, query: VectorSearchQuery
) -> bool:
    if db_context.dialect_name != "sqlite":
        return False
    if query.search_type not in {"semantic", "keyword", "hybrid"}:
        return False
    logger.warning(
        "Vector and full-text search are not supported with SQLite. "
        "Use PostgreSQL for these features."
    )
    return True


def _normalized_source_types(query: VectorSearchQuery) -> tuple[list[str], list[str]]:
    source_types = list(query.source_types)
    excluded_source_types = list(query.excluded_source_types)
    if query.source_ids:
        return source_types, excluded_source_types

    source_types = [
        source_type for source_type in source_types if source_type != "message_history"
    ]
    if "message_history" not in excluded_source_types:
        excluded_source_types.append("message_history")
    return source_types, excluded_source_types


def _build_not_in_clause_for_sqlite(
    params: dict[str, object],
    where_clauses: list[str],
    column_name: str,
    values: list[str],
    param_prefix: str,
) -> None:
    placeholders = ", ".join(f":{param_prefix}_{i}" for i in range(len(values)))
    for i, value in enumerate(values):
        params[f"{param_prefix}_{i}"] = value
    where_clauses.append(f"{column_name} NOT IN ({placeholders})")


def _add_source_filters(
    query: VectorSearchQuery,
    is_sqlite: bool,
    params: dict[str, object],
    clauses: list[str],
) -> None:
    source_types, excluded_source_types = _normalized_source_types(query)
    if source_types:
        if is_sqlite:
            _build_in_clause_for_sqlite(
                params, clauses, "d.source_type", source_types, "doc_source_type"
            )
        else:
            params["doc_source_types_array"] = source_types
            clauses.append("d.source_type = ANY(:doc_source_types_array)")

    if excluded_source_types:
        if is_sqlite:
            _build_not_in_clause_for_sqlite(
                params,
                clauses,
                "d.source_type",
                excluded_source_types,
                "doc_excluded_source_type",
            )
        else:
            params["doc_excluded_source_types_array"] = excluded_source_types
            clauses.append(
                "NOT (d.source_type = ANY(:doc_excluded_source_types_array))"
            )

    if query.source_ids:
        if is_sqlite:
            _build_in_clause_for_sqlite(
                params, clauses, "d.source_id", query.source_ids, "doc_source_id"
            )
        else:
            params["doc_source_ids_array"] = query.source_ids
            clauses.append("d.source_id = ANY(:doc_source_ids_array)")


def _add_document_value_filters(
    query: VectorSearchQuery,
    is_sqlite: bool,
    params: dict[str, object],
    clauses: list[str],
) -> None:
    if query.created_after:
        params["doc_created_gte"] = query.created_after
        clauses.append("d.created_at >= :doc_created_gte")
    if query.created_before:
        params["doc_created_lte"] = query.created_before
        clauses.append("d.created_at <= :doc_created_lte")
    if query.title_like:
        params["doc_title_ilike"] = f"%{query.title_like}%"
        like_operator = "LIKE" if is_sqlite else "ILIKE"
        clauses.append(f"d.title {like_operator} :doc_title_ilike")


def _add_metadata_filters(
    query: VectorSearchQuery,
    params: dict[str, object],
    clauses: list[str],
) -> None:
    for i, meta_filter in enumerate(query.metadata_filters):
        meta_key = meta_filter.key
        if not all(c.isalnum() or c in {"_", "-", "."} for c in meta_key):
            logger.warning(f"Potentially unsafe metadata key used: {meta_key}")

        param_name = f"doc_meta_value_{i}"
        params[param_name] = meta_filter.value
        clauses.append(f"d.doc_metadata->>'{meta_key}' = :{param_name}")


def _add_visibility_filter(
    query: VectorSearchQuery,
    params: dict[str, object],
    clauses: list[str],
) -> None:
    if query.visibility_grants is None:
        return
    params["visibility_grants_json"] = json.dumps(sorted(query.visibility_grants))
    clauses.append(
        "CAST(d.visibility_labels AS jsonb) <@ CAST(:visibility_grants_json AS jsonb)"
    )


def _build_document_filter_sql(
    query: VectorSearchQuery, is_sqlite: bool, params: dict[str, object]
) -> str:
    clauses = ["1=1"]
    _add_source_filters(query, is_sqlite, params, clauses)
    _add_document_value_filters(query, is_sqlite, params, clauses)
    _add_metadata_filters(query, params, clauses)
    _add_visibility_filter(query, params, clauses)
    return " AND ".join(clauses)


def _build_embedding_filter_sql(
    query: VectorSearchQuery, is_sqlite: bool, params: dict[str, object]
) -> str:
    clauses = ["1=1"]
    if not query.embedding_types:
        return clauses[0]
    if is_sqlite:
        _build_in_clause_for_sqlite(
            params, clauses, "de.embedding_type", query.embedding_types, "embed_type"
        )
    else:
        params["embed_types_array"] = query.embedding_types
        clauses.append("de.embedding_type = ANY(:embed_types_array)")
    return " AND ".join(clauses)


def _add_vector_search(
    parts: _SearchParts,
    query: VectorSearchQuery,
    query_embedding: list[float] | None,
    params: dict[str, object],
    doc_where_sql: str,
    embed_where_sql: str,
) -> None:
    if query.search_type not in {"semantic", "hybrid"}:
        return
    if not query.embedding_model:
        raise ValueError("embedding_model is missing for semantic search")

    params["query_embedding"] = query_embedding
    params["vector_model"] = query.embedding_model
    vector_limit = query.limit * 5
    parts.vector_cte = f"""
        vector_results AS (
          SELECT
              de_vec.id AS embedding_id,
              de_vec.document_id,
              de_vec.embedding <=> :query_embedding AS distance,
              ROW_NUMBER() OVER (ORDER BY de_vec.embedding <=> :query_embedding ASC) as vec_rank
          FROM document_embeddings de_vec
          WHERE de_vec.document_id IN (SELECT id FROM documents d WHERE {doc_where_sql})
            AND de_vec.embedding_model = :vector_model
            AND {embed_where_sql.replace("de.", "de_vec.")}
          ORDER BY distance ASC
          LIMIT {vector_limit}
        )
        """
    parts.joins.append("LEFT JOIN vector_results vr ON de.id = vr.embedding_id")
    parts.select_columns.extend(["vr.distance", "vr.vec_rank"])
    parts.result_predicates.append("vr.embedding_id IS NOT NULL")
    if query.search_type == "semantic":
        parts.order_by = "ORDER BY vr.distance ASC"


def _add_keyword_search(
    parts: _SearchParts,
    query: VectorSearchQuery,
    params: dict[str, object],
    doc_where_sql: str,
    embed_where_sql: str,
) -> None:
    if query.search_type not in {"keyword", "hybrid"}:
        return
    if not query.keywords:
        raise ValueError("keywords are missing for keyword search")

    params["query_keywords"] = query.keywords
    fts_limit = query.limit * 5
    parts.fts_cte = f"""
        fts_results AS (
          SELECT
              de_fts.id AS embedding_id,
              de_fts.document_id,
              ts_rank(to_tsvector('english', de_fts.content), plainto_tsquery('english', :query_keywords)) AS score,
              ROW_NUMBER() OVER (ORDER BY ts_rank(to_tsvector('english', de_fts.content), plainto_tsquery('english', :query_keywords)) DESC) as fts_rank
          FROM document_embeddings de_fts
          WHERE de_fts.document_id IN (SELECT id FROM documents d WHERE {doc_where_sql})
            AND de_fts.content IS NOT NULL
            AND to_tsvector('english', de_fts.content) @@ plainto_tsquery('english', :query_keywords)
            AND {embed_where_sql.replace("de.", "de_fts.")}
          ORDER BY score DESC
          LIMIT {fts_limit}
        )
        """
    parts.joins.append("LEFT JOIN fts_results fr ON de.id = fr.embedding_id")
    parts.select_columns.extend(["fr.score AS fts_score", "fr.fts_rank"])
    parts.result_predicates.append("fr.embedding_id IS NOT NULL")
    if query.search_type == "keyword":
        parts.order_by = "ORDER BY fr.score DESC"


def _configure_hybrid_ranking(parts: _SearchParts, query: VectorSearchQuery) -> str:
    if query.search_type != "hybrid":
        return " AND ".join(parts.result_predicates)
    parts.select_columns.append(
        "COALESCE(1.0 / (:rrf_k + vr.vec_rank), 0.0) + COALESCE(1.0 / (:rrf_k + fr.fts_rank), 0.0) AS rrf_score"
    )
    parts.order_by = "ORDER BY rrf_score DESC"
    return " OR ".join(parts.result_predicates)


def _combine_ctes(parts: _SearchParts) -> str:
    if parts.vector_cte and parts.fts_cte:
        return f"{parts.vector_cte} , {parts.fts_cte}"
    return parts.vector_cte or parts.fts_cte


def _build_search_sql(
    query: VectorSearchQuery,
    query_embedding: list[float] | None,
    params: dict[str, object],
    doc_where_sql: str,
    embed_where_sql: str,
) -> str | None:
    parts = _SearchParts()
    _add_vector_search(
        parts,
        query,
        query_embedding,
        params,
        doc_where_sql,
        embed_where_sql,
    )
    _add_keyword_search(parts, query, params, doc_where_sql, embed_where_sql)
    if not parts.vector_cte and not parts.fts_cte:
        return None

    final_where_sql = _configure_hybrid_ranking(parts, query)
    return f"""
    WITH {_combine_ctes(parts)}
    SELECT
        {", ".join(parts.select_columns)}
    FROM document_embeddings de
    JOIN documents d ON de.document_id = d.id
    {" ".join(parts.joins)}
    WHERE ({final_where_sql}) -- Ensure WHERE clause is valid even if empty
      AND ({doc_where_sql.replace("d.", "d.")}) -- Apply doc filters again on the final join result
      AND ({embed_where_sql.replace("de.", "de.")}) -- Apply embedding filters again
    {parts.order_by}
    LIMIT :limit;
    """


async def _execute_search(
    db_context: DatabaseExecutor,
    sql_query: str,
    params: dict[str, object],
    # ast-grep-ignore: no-dict-any - search results contain dynamic fields from joined tables
) -> list[dict[str, Any]]:
    logger.debug(f"Executing vector search query: {sql_query}")
    log_params = {
        key: value for key, value in params.items() if key != "query_embedding"
    }
    logger.debug(f"With params: {log_params}")
    try:
        results = await db_context.fetch_all(text(sql_query), params)
        return [dict(row) for row in results]
    except Exception as error:
        logger.exception(f"Error executing vector search query: {error}")
        raise


async def query_vector_store(
    db_context: DatabaseExecutor,
    query: VectorSearchQuery,
    query_embedding: list[float] | None = None,  # Pass generated embedding separately
    # ast-grep-ignore: no-dict-any - search results contain dynamic fields from joined tables
) -> list[dict[str, Any]]:
    """
    Performs vector, keyword, or hybrid search based on the VectorSearchQuery input.

    Args:
        db_context: The database context manager.
        query: The VectorSearchQuery object containing all parameters and filters.
        query_embedding: The vector embedding for semantic search (required if query.search_type
                         involves 'semantic').

    Returns:
        A list of dictionaries representing the search results.
    """
    _validate_query_embedding(query, query_embedding)
    is_sqlite = db_context.dialect_name == "sqlite"
    if _sqlite_search_is_unsupported(db_context, query):
        return []

    params: dict[str, object] = {"limit": query.limit, "rrf_k": query.rrf_k}
    doc_where_sql = _build_document_filter_sql(query, is_sqlite, params)
    embed_where_sql = _build_embedding_filter_sql(query, is_sqlite, params)
    sql_query = _build_search_sql(
        query, query_embedding, params, doc_where_sql, embed_where_sql
    )
    if sql_query is None:
        logger.warning("Search query doesn't involve vector or FTS components.")
        return []

    if "query_embedding" in params:
        params["query_embedding"] = str(params["query_embedding"])
    return await _execute_search(db_context, sql_query, params)


__all__ = ["MetadataFilter", "VectorSearchQuery", "query_vector_store"]
