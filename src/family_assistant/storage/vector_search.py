"""
Defines the schema for vector search queries and implements the query logic.
"""

import logging
from typing import List, Dict, Optional, Any, Literal
from dataclasses import dataclass, field
from datetime import datetime

from .context import DatabaseContext # Import context
from sqlalchemy.sql import text # For executing raw SQL if needed

logger = logging.getLogger(__name__)

# --- Input Schema Definition ---

@dataclass(frozen=True) # Use frozen=True for immutability if desired
class MetadataFilter:
    """Represents a simple key-value filter for JSONB metadata."""
    key: str
    value: str # Keep value as string for simplicity, conversion happens in query logic

@dataclass
class VectorSearchQuery:
    """
    Input schema for performing vector/keyword/hybrid searches.
    """
    search_type: Literal['semantic', 'keyword', 'hybrid'] = 'hybrid'

    # Query Content
    semantic_query: Optional[str] = None
    keywords: Optional[str] = None
    # Note: query_embedding is generated based on semantic_query and embedding_model,
    # so it's not part of the raw input schema but passed separately to the query function.

    # Model Selection (Required for semantic/hybrid)
    embedding_model: Optional[str] = None

    # Filters
    embedding_types: List[str] = field(default_factory=list)
    source_types: List[str] = field(default_factory=list)
    created_after: Optional[datetime] = None # Expect timezone-aware datetime
    created_before: Optional[datetime] = None # Expect timezone-aware datetime
    title_like: Optional[str] = None
    metadata_filter: Optional[MetadataFilter] = None

    # Control Parameters
    limit: int = 10
    rrf_k: int = 60 # Constant for Reciprocal Rank Fusion

    def __post_init__(self):
        """Basic validation."""
        if self.search_type in ['semantic', 'hybrid'] and not self.semantic_query:
            raise ValueError("semantic_query is required for 'semantic' or 'hybrid' search.")
        if self.search_type in ['semantic', 'hybrid'] and not self.embedding_model:
            raise ValueError("embedding_model is required for 'semantic' or 'hybrid' search.")
        if self.search_type == 'keyword' and not self.keywords:
            raise ValueError("keywords are required for 'keyword' search.")
        if self.limit <= 0:
            raise ValueError("limit must be positive.")
        if self.rrf_k <= 0:
            raise ValueError("rrf_k must be positive.")


# --- Query Function ---

async def query_vector_store(
    db_context: DatabaseContext,
    query: VectorSearchQuery,
    query_embedding: Optional[List[float]] = None, # Pass generated embedding separately
) -> List[Dict[str, Any]]:
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
    # --- Validation specific to embedding presence ---
    if query.search_type in ['semantic', 'hybrid'] and not query_embedding:
        raise ValueError("query_embedding is required for semantic/hybrid search execution.")

    # --- Parameter Mapping and Query Construction (using raw SQL example) ---
    params = {"limit": query.limit, "rrf_k": query.rrf_k}
    doc_where_clauses = ["1=1"]
    embed_where_clauses = ["1=1"]

    # Document Filters
    if query.source_types:
        params["doc_source_types"] = tuple(query.source_types)
        doc_where_clauses.append("d.source_type IN :doc_source_types")
    if query.created_after:
        params["doc_created_gte"] = query.created_after
        doc_where_clauses.append("d.created_at >= :doc_created_gte")
    if query.created_before:
        params["doc_created_lte"] = query.created_before
        doc_where_clauses.append("d.created_at <= :doc_created_lte")
    if query.title_like:
        params["doc_title_ilike"] = f"%{query.title_like}%" # Add wildcards here
        doc_where_clauses.append("d.title ILIKE :doc_title_ilike")
    if query.metadata_filter:
        meta_key = query.metadata_filter.key
        # Basic JSONB key/value filter ->>'key' = 'value'
        # WARNING: Directly embedding key might be risky if key comes from user input.
        # Parameterizing the key itself with standard libraries is tricky.
        # Ensure the key is validated/sanitized before embedding in the query string.
        # For now, assuming the key is safe or comes from a controlled source.
        if not meta_key.isalnum() and '_' not in meta_key: # Basic sanity check
             raise ValueError(f"Invalid metadata key format: {meta_key}")
        params["doc_meta_value"] = query.metadata_filter.value
        doc_where_clauses.append(f"d.doc_metadata->>'{meta_key}' = :doc_meta_value")

    doc_where_sql = " AND ".join(doc_where_clauses)

    # Embedding Filters
    if query.embedding_types:
        params["embed_types"] = tuple(query.embedding_types)
        embed_where_clauses.append("de.embedding_type IN :embed_types")
    # Model filter is applied within CTEs where needed

    embed_where_sql = " AND ".join(embed_where_clauses)

    # --- Build CTEs (similar logic as before, using query.* fields) ---
    vector_cte = ""
    fts_cte = ""
    final_select_cols = [
        "de.id AS embedding_id", "de.document_id", "d.title", "d.source_type",
        "d.created_at", "de.embedding_type", "de.content AS embedding_source_content",
        # Add other desired columns from 'd' or 'de'
        "d.source_id", "d.source_uri", "d.doc_metadata", "de.chunk_index"
    ]
    final_joins = []
    final_where = []
    final_order_by = ""

    # Vector Search CTE
    if query.search_type in ['semantic', 'hybrid']:
        if not query.embedding_model: # Should be caught by dataclass validation, but double-check
             raise ValueError("embedding_model is missing for semantic search")
        params["query_embedding"] = query_embedding
        params["vector_model"] = query.embedding_model
        distance_op = "<=>" # Assuming cosine
        vector_limit = query.limit * 5

        vector_cte = f"""
        vector_results AS (
          SELECT
              de_vec.id AS embedding_id,
              de_vec.document_id,
              de_vec.embedding {distance_op} :query_embedding AS distance,
              ROW_NUMBER() OVER (ORDER BY de_vec.embedding {distance_op} :query_embedding ASC) as vec_rank
          FROM document_embeddings de_vec
          WHERE de_vec.document_id IN (SELECT id FROM documents d WHERE {doc_where_sql})
            AND de_vec.embedding_model = :vector_model
            AND {embed_where_sql.replace('de.', 'de_vec.')}
          ORDER BY distance ASC
          LIMIT {vector_limit}
        )
        """
        final_joins.append("LEFT JOIN vector_results vr ON de.id = vr.embedding_id")
        final_select_cols.extend(["vr.distance", "vr.vec_rank"])
        final_where.append("vr.embedding_id IS NOT NULL")
        if query.search_type == "semantic":
            final_order_by = "ORDER BY vr.distance ASC"

    # FTS Search CTE
    if query.search_type in ['keyword', 'hybrid']:
        if not query.keywords: # Should be caught by dataclass validation
             raise ValueError("keywords are missing for keyword search")
        params["query_keywords"] = query.keywords
        fts_limit = query.limit * 5

        fts_cte = f"""
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
            AND {embed_where_sql.replace('de.', 'de_fts.')}
          ORDER BY score DESC
          LIMIT {fts_limit}
        )
        """
        final_joins.append("LEFT JOIN fts_results fr ON de.id = fr.embedding_id")
        final_select_cols.extend(["fr.score AS fts_score", "fr.fts_rank"])
        final_where.append("fr.embedding_id IS NOT NULL")
        if query.search_type == "keyword":
            final_order_by = "ORDER BY fr.score DESC"

    # Combine for Hybrid
    if query.search_type == "hybrid":
        final_select_cols.append(
            "COALESCE(1.0 / (:rrf_k + vr.vec_rank), 0.0) + COALESCE(1.0 / (:rrf_k + fr.fts_rank), 0.0) AS rrf_score"
        )
        final_where_sql = " OR ".join(final_where) # OR for hybrid
        final_order_by = "ORDER BY rrf_score DESC"
    else:
        final_where_sql = " AND ".join(final_where) # AND for single type


    # --- Construct Final Query ---
    # Ensure there's at least one CTE if we are joining/filtering based on them
    if not vector_cte and not fts_cte:
         # This case shouldn't happen if validation passes, but handle defensively
         logger.warning("Search query doesn't involve vector or FTS components.")
         # Construct a simpler query based only on filters if needed, or return empty
         # For now, return empty as the logic relies on CTEs
         return []

    # Convert embedding list to string format expected by pgvector for parameter binding
    if "query_embedding" in params:
        params["query_embedding"] = str(params["query_embedding"])

    # Need to select FROM the base tables and join CTEs
    sql_query = f"""
    WITH {vector_cte if vector_cte else ''} {' , ' if vector_cte and fts_cte else ''} {fts_cte if fts_cte else ''}
    SELECT
        {', '.join(final_select_cols)}
    FROM document_embeddings de
    JOIN documents d ON de.document_id = d.id
    {' '.join(final_joins)}
    WHERE ({final_where_sql}) -- Ensure WHERE clause is valid even if empty
      AND ({doc_where_sql.replace('d.', 'd.')}) -- Apply doc filters again on the final join result
      AND ({embed_where_sql.replace('de.', 'de.')}) -- Apply embedding filters again
    {final_order_by}
    LIMIT :limit;
    """

    # --- Execute ---
    logger.debug(f"Executing vector search query: {sql_query}")
    # Avoid logging embedding vector itself
    log_params = {k: v for k, v in params.items() if k != 'query_embedding'}
    logger.debug(f"With params: {log_params}")

    try:
        results = await db_context.fetch_all(text(sql_query), params)
        # Convert RowMapping objects (which behave like dicts) to actual dicts
        return [dict(row) for row in results]
    except Exception as e:
        logger.error(f"Error executing vector search query: {e}", exc_info=True)
        # Depending on desired behavior, either return empty list or re-raise
        raise # Re-raise the exception for the caller (web server) to handle


__all__ = ["VectorSearchQuery", "MetadataFilter", "query_vector_store"]
