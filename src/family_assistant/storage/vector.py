"""
API for interacting with the vector storage database (PostgreSQL with pgvector).

Handles storing document metadata, text chunks, and their corresponding vector embeddings.
Provides functions for adding, retrieving, deleting, and querying documents and embeddings.
"""

import logging
import os
from datetime import datetime
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Sequence,
    Protocol,
)

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Select,
    TextClause,
    and_,
    delete,
    func,
    select,
    text,
    literal_column,
    CTE,
    alias,
    cast,
    or_,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql.dml import OnConflictDoUpdate
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, aliased
from sqlalchemy.sql import functions  # Import functions explicitly
from sqlalchemy.sql.expression import ColumnElement
from sqlalchemy.engine import RowMapping

from pgvector.sqlalchemy import Vector  # type: ignore # noqa F401 - Needs to be imported for SQLAlchemy type mapping

# Use absolute package path
from family_assistant.storage.base import metadata, get_engine

logger = logging.getLogger(__name__)


# --- Database Setup ---


# --- Protocol Definition ---
class Document(Protocol):
    """Defines the interface for documents that can be ingested into vector storage.""" """Defines the interface for document objects that can be ingested into vector storage."""

    @property
    def source_type(self) -> str:
        """The type of the source (e.g., 'email', 'pdf', 'note')."""
        ...

    @property
    def source_id(self) -> str:
        """The unique identifier from the source system."""
        ...

    @property
    def source_uri(self) -> Optional[str]:
        """URI or path to the original item, if applicable."""
        ...

    @property
    def title(self) -> Optional[str]:
        """Title or subject of the document."""
        ...

    @property
    def created_at(self) -> Optional[datetime]:
        """Original creation date of the item (must be timezone-aware if provided)."""
        ...

    @property
    def metadata(self) -> Optional[Dict[str, Any]]:
        """Base metadata extracted directly from the source (can be enriched later)."""
        ...


class Base(DeclarativeBase):
    # Associate metadata with this Base
    metadata = metadata


class DocumentRecord(Base):
    """SQLAlchemy model for the 'documents' table, representing stored document metadata."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    source_type: Mapped[str] = mapped_column(sa.String(50), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    source_uri: Mapped[Optional[str]] = mapped_column(sa.Text)
    title: Mapped[Optional[str]] = mapped_column(sa.Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), index=True
    )
    added_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=functions.now(),  # Use explicit import
    )  # Use sa.sql.func.now() for server default
    doc_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )  # Use variant

    embeddings: Mapped[List["DocumentEmbeddingRecord"]] = sa.orm.relationship(
        "DocumentEmbeddingRecord",
        back_populates="document_record",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        sa.Index(
            "idx_documents_doc_metadata", doc_metadata, postgresql_using="gin"
        ),  # Updated index definition
    )


class DocumentEmbeddingRecord(Base):
    """SQLAlchemy model for the 'document_embeddings' table, representing stored embeddings."""

    __tablename__ = "document_embeddings"
    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    embedding_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(sa.Text)
    # Variable dimension vector requires pgvector >= 0.5.0
    embedding: Mapped[List[float]] = mapped_column(Vector, nullable=False)
    embedding_model: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(sa.Text)
    added_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=functions.now()
    )  # Use explicit import

    document_record: Mapped["DocumentRecord"] = sa.orm.relationship(
        "DocumentRecord", back_populates="embeddings"
    )

    __table_args__ = (
        sa.UniqueConstraint("document_id", "chunk_index", "embedding_type"),
        sa.Index("idx_doc_embeddings_type_model", embedding_type, embedding_model),
        sa.Index("idx_doc_embeddings_document_chunk", document_id, chunk_index),
        # GIN expression index for FTS is now created conditionally in init_vector_db
        # Example Partial HNSW index for a specific model/dimension
        # Requires manual creation via raw SQL in init_vector_db or migrations
        # sa.Index(
        #     "idx_doc_embeddings_gemini_1536_hnsw_cos",
        #     # Note: Need to explicitly use sa.text for the column expression part
        #     sa.text("(embedding::vector(1536)) vector_cosine_ops"), # Raw SQL for cast + opclass
        #     postgresql_using="hnsw",
        #     postgresql_where=sa.text("embedding_model = 'gemini-exp-03-07'"),
        #     postgresql_with={"m": 16, "ef_construction": 64},
        # ),
    )


# --- API Functions ---


async def init_vector_db():
    """Initializes the vector database components (extension, indexes). Tables are created by storage.init_db."""
    engine = get_engine()  # Use engine from db_base.py

    # Check if the dialect is PostgreSQL before running PG-specific commands
    if engine.dialect.name == "postgresql":
        logger.info(
            "PostgreSQL dialect detected. Initializing vector extension and indexes..."
        )
        async with engine.begin() as conn:
            # Tables are created via Base.metadata in storage.init_db
            # Manually create extensions and partial indexes that SQLAlchemy might not handle directly
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector;"))
            # Example: Create the specific partial index for gemini-exp-03-07
            # Note: Indexes might error if they already exist, add IF NOT EXISTS or handle errors
            try:
                await conn.execute(
                    sa.text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_doc_embeddings_gemini_1536_hnsw_cos ON document_embeddings
                USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
                WHERE embedding_model = 'gemini-exp-03-07'
                WITH (m = 16, ef_construction = 64);
                """
                    )
                )
                logger.info(
                    "Created/verified HNSW index idx_doc_embeddings_gemini_1536_hnsw_cos."
                )
            except Exception as e:
                # This might happen if the index exists but with different params, etc.
                # In a real app, consider more robust migration management.
                logger.warning(
                    f"Could not create partial HNSW index idx_doc_embeddings_gemini_1536_hnsw_cos: {e}"
                )

            # Also create the FTS index here
            try:
                await conn.execute(
                    sa.text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_doc_embeddings_content_fts_gin ON document_embeddings
                    USING gin (to_tsvector('english', content))
                    WHERE content IS NOT NULL;
                    """
                    )
                )
                logger.info(
                    "Created/verified FTS index idx_doc_embeddings_content_fts_gin."
                )
            except Exception as e:
                logger.warning(
                    f"Could not create FTS index idx_doc_embeddings_content_fts_gin: {e}"
                )

        logger.info(
            "PostgreSQL vector database components (extension, custom indexes) initialized."
        )
    else:
        logger.warning(
            f"Database dialect is '{engine.dialect.name}', not 'postgresql'. Skipping vector extension and index creation. Vector search functionality may be limited or unavailable."
        )


async def add_document(
    doc: Document,  # Use the renamed Protocol
    enriched_doc_metadata: Optional[  # Renamed parameter for clarity
        Dict[str, Any]  # Renamed parameter for clarity
    ] = None,  # Allow passing LLM enriched metadata
) -> int:
    """
    Adds a document record to the database or retrieves the existing one based on source_id.

    Uses the provided Document object (conforming to the protocol) to populate initial fields.
    Allows overriding or augmenting metadata with an optional enriched_metadata dictionary.

    Args:
        doc: An object conforming to the Document protocol (which has a .metadata property).
        enriched_doc_metadata: Optional dictionary containing metadata potentially enriched by an LLM,
                           which will be merged with or override the data from doc.metadata.

    Returns:
        The database ID of the added or existing document.
    engine = get_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    # Merge metadata
    final_doc_metadata = {**(doc.metadata or {}), **(enriched_doc_metadata or {})}

    values_to_insert = {
        "source_type": doc.source_type,
        "source_id": doc.source_id,
        "source_uri": doc.source_uri,
        "title": doc.title,
        "created_at": doc.created_at,
        "doc_metadata": final_doc_metadata,
    }

    # Prepare the insert statement with ON CONFLICT DO UPDATE
    # This requires PostgreSQL dialect features
    if engine.dialect.name != "postgresql":
        logger.error(
            "Database dialect is not PostgreSQL. ON CONFLICT clause is not supported. Document upsert might fail."
        )
        # Fallback or raise error - for now, let it potentially fail
        stmt = insert(DocumentRecord).values(**values_to_insert)
    else:
        stmt = insert(DocumentRecord).values(**values_to_insert)
        # Define columns to update on conflict
        update_dict = {
            col: getattr(stmt.excluded, col)
            for col in values_to_insert
            if col != "source_id"  # Don't update the conflict target
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_id"],  # The unique constraint column
            set_=update_dict,
        ).returning(DocumentRecord.id)

    try:
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(stmt)
                doc_id = result.scalar_one()  # Get the inserted or existing ID
                logger.info(
                    f"Successfully added/updated document with source_id {doc.source_id}, got ID: {doc_id}"
                )
                return doc_id
    except SQLAlchemyError as e:
        logger.error(
            f"Database error adding/updating document with source_id {doc.source_id}: {e}",
            exc_info=True,
        )
        raise  # Re-raise the exception after logging
    except Exception as e:
        logger.error(
            f"Unexpected error adding/updating document with source_id {doc.source_id}: {e}",
            exc_info=True,
        )
        raise


async def get_document_by_source_id(source_id: str) -> Optional[DocumentRecord]:
    """Retrieves a document ORM object by its source ID."""
    engine = get_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    stmt = select(DocumentRecord).where(DocumentRecord.source_id == source_id)

    try:
        async with async_session() as session:
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if record:
                logger.debug(f"Found document with source_id {source_id}")
                return record
            else:
                logger.debug(f"No document found with source_id {source_id}")
                return None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error retrieving document with source_id {source_id}: {e}",
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error retrieving document with source_id {source_id}: {e}",
            exc_info=True,
        )
        raise


async def add_embedding(
    document_id: int,
    chunk_index: int,
    embedding_type: str,
    embedding: List[float],
    embedding_model: str,
    content: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> None:
    """Adds an embedding record linked to a document, updating if it already exists."""
    engine = get_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    values_to_insert = {
        "document_id": document_id,
        "chunk_index": chunk_index,
        "embedding_type": embedding_type,
        "embedding": embedding,
        "embedding_model": embedding_model,
        "content": content,
        "content_hash": content_hash,
    }

    if engine.dialect.name != "postgresql":
        logger.error(
            "Database dialect is not PostgreSQL. ON CONFLICT clause is not supported. Embedding upsert might fail."
        )
        # Fallback or raise error
        stmt = insert(DocumentEmbeddingRecord).values(**values_to_insert)
    else:
        stmt = insert(DocumentEmbeddingRecord).values(**values_to_insert)
        # Define columns to update on conflict
        update_dict = {
            col: getattr(stmt.excluded, col)
            for col in values_to_insert
            if col not in ["document_id", "chunk_index", "embedding_type"]
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["document_id", "chunk_index", "embedding_type"],
            set_=update_dict,
        )

    try:
        async with async_session() as session:
            async with session.begin():
                await session.execute(stmt)
                logger.info(
                    f"Successfully added/updated embedding for doc {document_id}, chunk {chunk_index}, type {embedding_type}"
                )
    except SQLAlchemyError as e:
        logger.error(
            f"Database error adding/updating embedding for doc {document_id}, chunk {chunk_index}, type {embedding_type}: {e}",
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error adding/updating embedding for doc {document_id}, chunk {chunk_index}, type {embedding_type}: {e}",
            exc_info=True,
        )
        raise


async def delete_document(document_id: int) -> bool:
    """
    Deletes a document and its associated embeddings (via CASCADE constraint).

    Returns:
        True if a document was deleted, False otherwise.
    """
    engine = get_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    stmt = delete(DocumentRecord).where(DocumentRecord.id == document_id)

    try:
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(stmt)
                deleted_count = result.rowcount
                if deleted_count > 0:
                    logger.info(
                        f"Successfully deleted document {document_id} and its embeddings (via cascade)."
                    )
                    return True
                else:
                    logger.warning(f"No document found with ID {document_id} to delete.")
                    return False
    except SQLAlchemyError as e:
        logger.error(
            f"Database error deleting document {document_id}: {e}", exc_info=True
        )
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error deleting document {document_id}: {e}", exc_info=True
        )
        raise


async def query_vectors(
    query_embedding: List[float],
    embedding_model: str,  # Used to select the correct index and filter
    keywords: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,  # For document table filtering
    embedding_type_filter: Optional[List[str]] = None,  # Filter embeddings by type
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Performs a hybrid search combining vector similarity and keyword search
    with metadata filtering.

    Args:
        query_embedding: The vector representation of the search query.
        embedding_model: Identifier of the model used for the query vector (must match indexed models).
        keywords: Keywords for full-text search.
        filters: Dictionary of filters to apply to the 'documents' table
                 (e.g., {"source_type": "email", "created_at_gte": datetime(...)})
        embedding_type_filter: List of allowed embedding types to search within.
        limit: The maximum number of results to return.

    Returns:
        A list of dictionaries, each representing a relevant document chunk/embedding
        with its metadata and scores.
    """
    engine = get_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    if engine.dialect.name != "postgresql":
        logger.error(
            "Vector search query requires PostgreSQL dialect. Skipping query."
        )
        return []

    # --- 1. Build Document Filter ---
    doc_filter_conditions = []
    if filters:
        for key, value in filters.items():
            if hasattr(DocumentRecord, key):
                column = getattr(DocumentRecord, key)
                # Handle common filter types (add more as needed)
                if isinstance(value, (str, int, bool)):
                    doc_filter_conditions.append(column == value)
                elif key.endswith("_gte") and isinstance(value, datetime):
                    actual_key = key[:-4]
                    if hasattr(DocumentRecord, actual_key):
                        doc_filter_conditions.append(getattr(DocumentRecord, actual_key) >= value)
                elif key.endswith("_lte") and isinstance(value, datetime):
                    actual_key = key[:-4]
                    if hasattr(DocumentRecord, actual_key):
                         doc_filter_conditions.append(getattr(DocumentRecord, actual_key) <= value)
                # Add more complex filter handling here (e.g., IN, JSONB contains)
            else:
                logger.warning(f"Ignoring unknown filter key: {key}")

    doc_filter = and_(*doc_filter_conditions) if doc_filter_conditions else sa.true()

    # --- 2. Build Embedding Filter ---
    embedding_filter_conditions = [
        DocumentEmbeddingRecord.embedding_model == embedding_model
    ]
    if embedding_type_filter:
        embedding_filter_conditions.append(
            DocumentEmbeddingRecord.embedding_type.in_(embedding_type_filter)
        )
    embedding_filter = and_(*embedding_filter_conditions)

    # --- 3. Vector Search CTE ---
    # Determine distance operator based on common practice for models (cosine often preferred)
    # This might need adjustment based on the specific model and index used.
    # Assuming cosine distance (<=>) for this example.
    # IMPORTANT: The dimension in cast() MUST match the query_embedding and the index definition
    # We cannot easily get dimension from model name here, so we rely on the caller providing
    # a query_embedding with the correct dimension that matches the index being used.
    # A more robust solution might involve storing dimensions with model names.
    distance_op = DocumentEmbeddingRecord.embedding.cosine_distance # Example, adjust as needed
    # distance_op = DocumentEmbeddingRecord.embedding.l2_distance
    # distance_op = DocumentEmbeddingRecord.embedding.max_inner_product

    vector_subquery = (
        select(
            DocumentEmbeddingRecord.id.label("embedding_id"),
            DocumentEmbeddingRecord.document_id,
            distance_op(query_embedding).label("distance"),
        )
        .join(DocumentRecord, DocumentEmbeddingRecord.document_id == DocumentRecord.id)
        .where(doc_filter)
        .where(embedding_filter)
        .order_by(literal_column("distance").asc())
        .limit(limit * 5) # Retrieve more candidates for RRF, adjust multiplier as needed
        .subquery("vector_subquery") # Use subquery first
    )

    vector_results_cte = select(
        vector_subquery.c.embedding_id,
        vector_subquery.c.document_id,
        vector_subquery.c.distance,
        func.row_number().over(order_by=vector_subquery.c.distance.asc()).label("vec_rank")
    ).cte("vector_results")


    # --- 4. FTS Search CTE (Conditional) ---
    fts_results_cte = None
    if keywords:
        # Use the GIN index expression directly in the query
        tsvector_col = func.to_tsvector('english', DocumentEmbeddingRecord.content)
        tsquery = func.plainto_tsquery('english', keywords)

        fts_subquery = (
            select(
                DocumentEmbeddingRecord.id.label("embedding_id"),
                DocumentEmbeddingRecord.document_id,
                func.ts_rank(tsvector_col, tsquery).label("score"),
            )
            .join(DocumentRecord, DocumentEmbeddingRecord.document_id == DocumentRecord.id)
            .where(doc_filter)
            # Optionally apply embedding_filter here too if FTS should be restricted
            # .where(embedding_filter)
            .where(DocumentEmbeddingRecord.content != None) # Ensure content exists for FTS
            .where(tsvector_col.op("@@")(tsquery)) # Use the @@ operator for matching
            .order_by(literal_column("score").desc())
            .limit(limit * 5) # Retrieve more candidates for RRF
            .subquery("fts_subquery")
        )

        fts_results_cte = select(
            fts_subquery.c.embedding_id,
            fts_subquery.c.document_id,
            fts_subquery.c.score,
            func.row_number().over(order_by=fts_subquery.c.score.desc()).label("fts_rank")
        ).cte("fts_results")


    # --- 5. Final Query Construction ---
    final_select_cols = [
        DocumentEmbeddingRecord.id.label("embedding_id"),
        DocumentEmbeddingRecord.document_id,
        DocumentRecord.title,
        DocumentRecord.source_type,
        DocumentRecord.source_id,
        DocumentRecord.source_uri,
        DocumentRecord.created_at,
        DocumentRecord.doc_metadata,
        DocumentEmbeddingRecord.embedding_type,
        DocumentEmbeddingRecord.content.label("embedding_source_content"),
        DocumentEmbeddingRecord.chunk_index,
        vector_results_cte.c.distance,
        vector_results_cte.c.vec_rank,
    ]

    # Base query joining documents and embeddings
    final_query = select(*final_select_cols).select_from(
        DocumentEmbeddingRecord
    ).join(
        DocumentRecord, DocumentEmbeddingRecord.document_id == DocumentRecord.id
    )

    # Join with Vector CTE
    final_query = final_query.join(
        vector_results_cte,
        DocumentEmbeddingRecord.id == vector_results_cte.c.embedding_id,
        isouter=True, # Left join
    )

    # Conditionally join with FTS CTE and add FTS columns/score
    if fts_results_cte is not None:
        final_select_cols.extend([
            fts_results_cte.c.score.label("fts_score"),
            fts_results_cte.c.fts_rank,
        ])
        final_query = final_query.join(
            fts_results_cte,
            DocumentEmbeddingRecord.id == fts_results_cte.c.embedding_id,
            isouter=True, # Left join
        )
        # RRF Score Calculation (k=60 is common default)
        rrf_score = (
            func.coalesce(1.0 / (60 + vector_results_cte.c.vec_rank), 0.0) +
            func.coalesce(1.0 / (60 + fts_results_cte.c.fts_rank), 0.0)
        ).label("rrf_score")
        final_select_cols.append(rrf_score)
        # Filter: Must appear in at least one result set
        final_query = final_query.where(
            or_(
                vector_results_cte.c.embedding_id != None,
                fts_results_cte.c.embedding_id != None
            )
        )
        final_query = final_query.order_by(rrf_score.desc())
    else:
        # If no FTS, just filter by vector results and order by distance
        final_query = final_query.where(vector_results_cte.c.embedding_id != None)
        final_query = final_query.order_by(vector_results_cte.c.distance.asc()) # Or vec_rank

    # Apply final limit
    final_query = final_query.limit(limit)

    # Update the select clause with potentially added columns
    final_query = final_query.with_only_columns(*final_select_cols)


    # --- 6. Execute and Return ---
    try:
        async with async_session() as session:
            logger.debug(f"Executing vector search query: {final_query}")
            result = await session.execute(final_query)
            rows = result.mappings().all()
            logger.info(f"Vector query returned {len(rows)} results.")
            # Convert RowMappings to plain dicts
            return [dict(row) for row in rows]
    except SQLAlchemyError as e:
        logger.error(f"Database error during vector query: {e}", exc_info=True)
        # You might want to inspect the compiled query in case of SQL errors:
        # from sqlalchemy.dialects import postgresql
        # logger.error(f"Compiled query: {final_query.compile(dialect=postgresql.dialect())}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during vector query: {e}", exc_info=True)
        raise


# Export functions explicitly for clarity when importing elsewhere
__all__ = [
    "init_vector_db",
    "add_document",
    "get_document_by_source_id",
    "add_embedding",
    "delete_document",
    "query_vectors",
    "DocumentRecord",  # Export SQLAlchemy ORM model
    "DocumentEmbeddingRecord",  # Export SQLAlchemy ORM model
    "Document",  # Export the protocol
]
