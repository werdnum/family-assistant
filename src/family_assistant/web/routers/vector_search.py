import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector_search import (
    MetadataFilter,
    VectorSearchQuery,
    query_vector_store,
)
from family_assistant.storage.vector import DocumentRecord, get_document_by_id
from family_assistant.web.auth import AUTH_ENABLED
from family_assistant.web.dependencies import (
    get_db,
    get_embedding_generator_dependency,
)
from family_assistant.web.models import SearchResultItem

logger = logging.getLogger(__name__)
vector_search_router = APIRouter()


@vector_search_router.get(
    "/vector-search", response_class=HTMLResponse, name="ui_vector_search"
)
async def vector_search_form(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> HTMLResponse:
    """Serves the vector search form."""
    templates = request.app.state.templates
    distinct_models = []
    distinct_types = []
    distinct_source_types = []
    distinct_metadata_keys = []  # Added for metadata keys
    error = None
    try:
        # Fetch distinct values for dropdowns/filters
        # Ensure table/column names match your actual schema
        q_models = text(
            "SELECT DISTINCT embedding_model FROM document_embeddings ORDER BY embedding_model;"
        )
        q_types = text(
            "SELECT DISTINCT embedding_type FROM document_embeddings ORDER BY embedding_type;"
        )
        q_source_types = text(
            "SELECT DISTINCT source_type FROM documents ORDER BY source_type;"
        )
        # Query to get distinct top-level keys from the JSONB metadata column
        q_meta_keys = text(
            "SELECT DISTINCT key FROM documents, jsonb_object_keys(doc_metadata) AS keys(key) ORDER BY key;"
        )

        (
            models_result,
            types_result,
            source_types_result,
            meta_keys_result,
        ) = await asyncio.gather(
            db_context.fetch_all(q_models),
            db_context.fetch_all(q_types),
            db_context.fetch_all(q_source_types),
            db_context.fetch_all(q_meta_keys),  # Fetch metadata keys
        )

        distinct_models = [row["embedding_model"] for row in models_result]
        distinct_types = [row["embedding_type"] for row in types_result]
        distinct_source_types = [row["source_type"] for row in source_types_result]
        distinct_metadata_keys = [
            row["key"] for row in meta_keys_result
        ]  # Populate metadata keys

    except Exception as e:
        logger.error(
            f"Failed to fetch distinct values for search form: {e}", exc_info=True
        )
        error = "Could not load filter options from database."
        # Continue without pre-populated dropdowns

    return templates.TemplateResponse(
        "vector_search.html",
        {
            "request": request,
            "results": None,
            "search_params": {},  # Empty for initial GET
            "error": error,
            "distinct_models": distinct_models,
            "distinct_types": distinct_types,
            "distinct_source_types": distinct_source_types,
            "distinct_metadata_keys": distinct_metadata_keys,  # Pass keys to template
            "user": request.session.get("user"),
            "AUTH_ENABLED": AUTH_ENABLED,  # Pass to base template
            "now_utc": datetime.now(timezone.utc),  # Pass to base template
        },
    )


@vector_search_router.get(
    "/vector-search/document/{document_id}",
    response_class=HTMLResponse,
    name="ui_document_detail",
)
async def document_detail_view(
    request: Request,
    document_id: int,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> HTMLResponse:
    """Serves the detailed view for a single document."""
    templates = request.app.state.templates
    document: DocumentRecord | None = None
    error: str | None = None

    try:
        document = await get_document_by_id(db_context, document_id)
        if not document:
            error = f"Document with ID {document_id} not found."
    except Exception as e:
        logger.error(f"Error fetching document {document_id}: {e}", exc_info=True)
        error = f"An error occurred while fetching document details: {e}"

    return templates.TemplateResponse(
        "document_detail.html",
        {
            "request": request,
            "document": document,
            "error": error,
            "user": request.session.get("user"),
            "AUTH_ENABLED": AUTH_ENABLED,
            "now_utc": datetime.now(timezone.utc),
        },
    )


@vector_search_router.post(
    "/vector-search", response_class=HTMLResponse, name="ui_vector_search_post"
)  # Add name for POST if needed, or ensure GET is named ui_vector_search
async def handle_vector_search(
    request: Request,
    # --- Dependencies ---
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    embedding_generator: Annotated[
        EmbeddingGenerator, Depends(get_embedding_generator_dependency)
    ],
    # --- Form Inputs ---
    semantic_query: Annotated[str | None, Form()] = None,
    keywords: Annotated[str | None, Form()] = None,
    search_type: Annotated[str, Form()] = "hybrid",  # 'semantic', 'keyword', 'hybrid'
    embedding_model: Annotated[str | None, Form()] = None,  # CRUCIAL for vector search
    embedding_types: Annotated[list[str], Form()] = None,  # Allow multiple types
    source_types: Annotated[list[str], Form()] = None,  # Allow multiple source types
    created_after: Annotated[str | None, Form()] = None,  # Expect YYYY-MM-DD
    created_before: Annotated[str | None, Form()] = None,  # Expect YYYY-MM-DD
    title_like: Annotated[str | None, Form()] = None,
    # --- Metadata Filters (expect lists) ---
    metadata_keys: Annotated[list[str], Form()] = None,
    metadata_values: Annotated[list[str], Form()] = None,
    # --- Control Params ---
    limit: Annotated[int, Form()] = 10,
    rrf_k: Annotated[int, Form()] = 60,
) -> HTMLResponse:
    """Handles the vector search form submission."""
    templates = request.app.state.templates
    if metadata_values is None:
        metadata_values = []
    if metadata_keys is None:
        metadata_keys = []
    if source_types is None:
        source_types = []
    if embedding_types is None:
        embedding_types = []
    results = None
    error = None
    query_embedding = None

    # --- Default keywords to semantic query if keywords are empty ---
    effective_keywords = keywords
    if not keywords and semantic_query:
        effective_keywords = semantic_query
        logger.info(
            f"Keywords field was empty, defaulting to semantic query: '{semantic_query}'"
        )

    search_params = {  # Store params to repopulate form
        "semantic_query": semantic_query,
        "keywords": keywords,  # Store original keywords for form
        "search_type": search_type,
        "embedding_model": embedding_model,
        "embedding_types": embedding_types,
        "source_types": source_types,
        "created_after": created_after,
        "created_before": created_before,
        "title_like": title_like,
        # Store lists for metadata filters
        "metadata_keys": metadata_keys,
        "metadata_values": metadata_values,
        "limit": limit,
        "rrf_k": rrf_k,
    }
    distinct_models = []  # Fetch again or pass from GET state if possible
    distinct_types = []
    distinct_source_types = []
    distinct_metadata_keys = []  # Fetch again

    try:
        # --- Parse Dates (handle potential errors) ---
        created_after_dt: datetime | None = None
        if created_after:
            try:
                # Assume YYYY-MM-DD, make it timezone-aware (start of day UTC)
                created_after_dt = datetime.strptime(created_after, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                raise ValueError(
                    "Invalid 'Created After' date format. Use YYYY-MM-DD."
                ) from None
        created_before_dt: datetime | None = None
        if created_before:
            try:
                # Assume YYYY-MM-DD, make it timezone-aware (end of day UTC)
                # Add 1 day and subtract epsilon or use < comparison in SQL
                # Simpler: use the date directly, SQL query uses <=
                created_before_dt = datetime.strptime(
                    created_before, "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                raise ValueError(
                    "Invalid 'Created Before' date format. Use YYYY-MM-DD."
                ) from None

        # --- Build List of Metadata Filters ---
        metadata_filters_list: list[MetadataFilter] = []
        if len(metadata_keys) != len(metadata_values):
            # This indicates a potential issue with form submission or client-side JS
            logger.error(
                f"Mismatch between metadata keys ({len(metadata_keys)}) and values ({len(metadata_values)}). Ignoring metadata filters."
            )
            error = "Error: Mismatch in metadata filter keys and values."
            # Optionally clear the lists to prevent partial filtering
            metadata_keys = []
            metadata_values = []
        else:
            for key, value in zip(metadata_keys, metadata_values, strict=False):
                if (
                    key and value is not None
                ):  # Allow empty string value, but require key
                    metadata_filters_list.append(MetadataFilter(key=key, value=value))
                elif key and value is None:
                    logger.warning(
                        f"Metadata key '{key}' provided without a value. Ignoring this filter."
                    )
                # No warning needed if key is empty, as it's likely from an empty template row

        # --- Create Query Object ---
        # Validation is handled by the dataclass __post_init__
        query_obj = VectorSearchQuery(
            search_type=search_type,
            semantic_query=semantic_query,
            keywords=effective_keywords,  # Use the potentially defaulted keywords
            embedding_model=embedding_model,
            embedding_types=embedding_types,
            source_types=source_types,
            created_after=created_after_dt,
            created_before=created_before_dt,
            title_like=title_like,
            metadata_filters=metadata_filters_list,  # Pass the list
            limit=limit,
            rrf_k=rrf_k,
        )

        # --- Generate Embedding ---
        if query_obj.search_type in ["semantic", "hybrid"]:
            # Basic check, might need more robust model matching/selection
            embedding_result = await embedding_generator.generate_embeddings(
                [query_obj.semantic_query]
            )  # Pass as list
            if not embedding_result.embeddings or len(embedding_result.embeddings) == 0:
                raise ValueError("Failed to generate embedding for the semantic query.")
            query_embedding = embedding_result.embeddings[0]
            # Optional: Check dimension if needed by query_vector_store

        # --- Execute Search ---
        raw_results = await query_vector_store(
            db_context=db_context,
            query=query_obj,
            query_embedding=query_embedding,
        )
        # Convert raw results (dicts) to Pydantic models for consistency (optional)
        results = [SearchResultItem.parse_obj(row) for row in raw_results]

    except ValueError as ve:
        logger.warning(f"Validation error during vector search: {ve}")
        error = str(ve)
    except NotImplementedError as nie:
        logger.error(f"Configuration error: {nie}")
        error = f"Server configuration error: {nie}"
    except Exception as e:
        logger.error(f"Error during vector search: {e}", exc_info=True)
        error = f"An unexpected error occurred: {e}"

    # Fetch distinct values again for rendering the form correctly
    # TODO: Consider caching these or passing via state if performance is an issue
    try:
        q_models = text(
            "SELECT DISTINCT embedding_model FROM document_embeddings ORDER BY embedding_model;"
        )
        q_types = text(
            "SELECT DISTINCT embedding_type FROM document_embeddings ORDER BY embedding_type;"
        )
        q_source_types = text(
            "SELECT DISTINCT source_type FROM documents ORDER BY source_type;"
        )
        q_meta_keys = text(
            "SELECT DISTINCT key FROM documents, jsonb_object_keys(doc_metadata) AS keys(key) ORDER BY key;"
        )

        # Use asyncio.gather for concurrent fetching
        (
            models_result,
            types_result,
            source_types_result,
            meta_keys_result,
        ) = await asyncio.gather(
            db_context.fetch_all(q_models),
            db_context.fetch_all(q_types),
            db_context.fetch_all(q_source_types),
            db_context.fetch_all(q_meta_keys),
        )

        distinct_models = [row["embedding_model"] for row in models_result]
        distinct_types = [row["embedding_type"] for row in types_result]
        distinct_source_types = [row["source_type"] for row in source_types_result]
        distinct_metadata_keys = [row["key"] for row in meta_keys_result]  # Get keys

    except Exception as e:
        logger.error(
            f"Failed to fetch distinct values for search form render: {e}",
            exc_info=True,
        )
        # Don't overwrite previous error, but log this one
        if not error:
            error = "Could not load filter options from database."

    return templates.TemplateResponse(
        "vector_search.html",
        {
            "request": request,
            "results": results,
            "search_params": search_params,  # Pass back params
            "error": error,
            "distinct_models": distinct_models,
            "distinct_types": distinct_types,
            "distinct_source_types": distinct_source_types,
            "distinct_metadata_keys": distinct_metadata_keys,  # Pass keys
            "user": request.session.get("user"),
            "AUTH_ENABLED": AUTH_ENABLED,  # Pass to base template
            "now_utc": datetime.now(timezone.utc),  # Pass to base template
        },
    )
