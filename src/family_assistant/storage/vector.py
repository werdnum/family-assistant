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
from family_assistant.storage.base import metadata  # Keep metadata

# Remove get_engine import
from family_assistant.storage.context import DatabaseContext  # Import DatabaseContext

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
        #     "idx_doc_embeddings_gemini_exp_03_07_hnsw_cos", # Updated index name example
        #     # Note: Need to explicitly use sa.text for the column expression part
        #     # Adjust vector dimension (e.g., 768) if needed for the new model
        #     sa.text("(embedding::vector(768)) vector_cosine_ops"), # Raw SQL for cast + opclass
        #     postgresql_using="hnsw",
        #     postgresql_where=sa.text("embedding_model = 'gemini/gemini-embedding-exp-03-07'"), # Updated model name
        #     postgresql_with={"m": 16, "ef_construction": 64},
        # ),
    )


# --- API Functions ---


async def init_vector_db(db_context: DatabaseContext):
    """
    Initializes the vector database components (extension, indexes) using the provided context.
    Tables should be created separately via storage.init_db or metadata.create_all.

    Args:
        db_context: The DatabaseContext to use for executing initialization commands.
    """
    # Check if the dialect is PostgreSQL before running PG-specific commands
    # Access the engine from the context
    if db_context.engine.dialect.name == "postgresql":
        logger.info(
            "PostgreSQL dialect detected. Initializing vector extension and indexes..."
        )
        try:
            # Use execute_with_retry for DDL statements within the context
            # Commit/rollback is handled by the context manager (__aexit__)
            # Only create the extension here. Indexes will be created separately after tables exist.
            await db_context.execute_with_retry(
                sa.text("CREATE EXTENSION IF NOT EXISTS vector;")
            )
            logger.info("Ensured 'vector' extension exists.")

        except SQLAlchemyError as e:
            # Catch potential errors during extension creation
            logger.error(
                f"Database error during vector DB initialization: {e}", exc_info=True
            )
            raise  # Re-raise to indicate failure

        logger.info("PostgreSQL vector database extension initialized.")
    else:
        logger.warning(
            f"Database dialect is '{db_context.engine.dialect.name}', not 'postgresql'. Skipping vector extension and index creation. Vector search functionality may be limited or unavailable."
        )


async def add_document(
    db_context: DatabaseContext,  # Added context
    doc: Document,
    enriched_doc_metadata: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Adds a document record to the database or updates it based on source_id.

    Args:
        db_context: The DatabaseContext to use for the operation.
        doc: An object conforming to the Document protocol.
        enriched_doc_metadata: Optional dictionary containing enriched metadata.

    Returns:
        The database ID of the added or existing document.
    """
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
    if db_context.engine.dialect.name != "postgresql":
        logger.error(
            "Database dialect is not PostgreSQL. ON CONFLICT clause is not supported. Document upsert might fail."
        )
        # Fallback or raise error - for now, let it potentially fail
        stmt = insert(DocumentRecord).values(**values_to_insert)
        # Need to fetch ID separately if ON CONFLICT is not used
        # This part is tricky without ON CONFLICT returning the ID
        raise NotImplementedError(
            "add_document without ON CONFLICT returning ID is not fully implemented."
        )
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
        # Use execute_with_retry as commit is handled by context manager
        result = await db_context.execute_with_retry(stmt)
        doc_id = result.scalar_one()  # Get the inserted or existing ID
        logger.info(
            f"Successfully added/updated document with source_id {doc.source_id}, got ID: {doc_id}"
        )
        # Commit happens implicitly if context manager is used, or explicitly if needed
        # Assuming execute_with_retry doesn't commit automatically unless execute_and_commit is used
        # Let's assume the caller manages the transaction boundary.
        # If this function should be atomic, it needs to manage the transaction.
        # For now, assume caller handles transaction.
        return doc_id
    except SQLAlchemyError as e:
        logger.error(
            f"Database error adding/updating document with source_id {doc.source_id}: {e}",
            exc_info=True,
        )
        raise


async def get_document_by_source_id(
    db_context: DatabaseContext, source_id: str  # Added context
) -> Optional[DocumentRecord]:
    """Retrieves a document ORM object by its source ID."""
    try:
        # Use ORM select with the context's session if available, or execute directly
        # For simplicity with context, using core select
        stmt = select(DocumentRecord).where(DocumentRecord.source_id == source_id)
        # fetch_one returns a dict-like mapping, need to reconstruct ORM object if required
        # Or, use context's session if it provided one (needs context enhancement)
        # Let's stick to core API for now:
        result_mapping = await db_context.fetch_one(stmt)
        if result_mapping:
            # Manually create ORM object (less ideal)
            # record = DocumentRecord(**result_mapping) # This might fail with relationships etc.
            # Alternative: If the API contract allows returning the dict, do that.
            # For now, let's assume the ORM object is needed and this needs refinement
            # or the context needs session support.
            # Returning the raw mapping for now.
            logger.warning(
                "get_document_by_source_id returning raw mapping, not ORM object due to context limitations."
            )
            # To return ORM object, context needs session support or use Session directly.
            # Let's fetch using sessionmaker for ORM compatibility
            async_session = async_sessionmaker(
                db_context.engine, expire_on_commit=False
            )
            async with async_session() as session:
                result = await session.execute(stmt)
                record = result.scalar_one_or_none()
                if record:
                    logger.debug(f"Found document with source_id {source_id}")
                    return record
                else:
                    logger.debug(f"No document found with source_id {source_id}")
                    return None

        else:
            logger.debug(f"No document found with source_id {source_id}")
            return None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error retrieving document with source_id {source_id}: {e}",
            exc_info=True,
        )
        raise


async def add_embedding(
    db_context: DatabaseContext,  # Added context
    document_id: int,
    chunk_index: int,
    embedding_type: str,
    embedding: List[float],
    embedding_model: str,
    content: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> None:
    """Adds an embedding record linked to a document, updating if it already exists."""
    values_to_insert = {
        "document_id": document_id,
        "chunk_index": chunk_index,
        "embedding_type": embedding_type,
        "embedding": embedding,
        "embedding_model": embedding_model,
        "content": content,
        "content_hash": content_hash,
    }

    if db_context.engine.dialect.name != "postgresql":
        logger.error(
            "Database dialect is not PostgreSQL. ON CONFLICT clause is not supported. Embedding upsert might fail."
        )
        # Fallback or raise error
        stmt = insert(DocumentEmbeddingRecord).values(**values_to_insert)
        # This won't handle updates on conflict
        raise NotImplementedError(
            "add_embedding without ON CONFLICT is not fully implemented."
        )

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
        # Use execute_with_retry as commit is handled by context manager
        await db_context.execute_with_retry(stmt)
        logger.info(
            f"Successfully added/updated embedding for doc {document_id}, chunk {chunk_index}, type {embedding_type}"
        )
    except SQLAlchemyError as e:
        logger.error(
            f"Database error adding/updating embedding for doc {document_id}, chunk {chunk_index}, type {embedding_type}: {e}",
            exc_info=True,
        )
        raise


async def delete_document(db_context: DatabaseContext, document_id: int) -> bool:
    """
    Deletes a document and its associated embeddings (via CASCADE constraint).

    Returns:
        True if a document was deleted, False otherwise.
    """
    try:
        stmt = delete(DocumentRecord).where(DocumentRecord.id == document_id)
        # Use execute_with_retry as commit is handled by context manager
        result = await db_context.execute_with_retry(stmt)
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


async def query_vectors(
    db_context: DatabaseContext,  # Added context
    query_embedding: List[float],
    embedding_model: str,
    keywords: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
    embedding_type_filter: Optional[List[str]] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Performs a hybrid search combining vector similarity and keyword search
    with metadata filtering using the provided DatabaseContext.

    Args:
        db_context: The DatabaseContext to use for the query.
        query_embedding: The vector representation of the search query.
        embedding_model: Identifier of the model used for the query vector.
        keywords: Keywords for full-text search.
        filters: Dictionary of filters for the 'documents' table.
        embedding_type_filter: List of allowed embedding types.
        limit: The maximum number of results.

    Returns:
        A list of dictionaries representing relevant document embeddings.
    """
    if db_context.engine.dialect.name != "postgresql":
        logger.error("Vector search query requires PostgreSQL dialect. Skipping query.")
        return []

    # --- Query construction remains largely the same ---
    # --- 1. Build Document Filter ---
    doc_filter_conditions = []
    if filters:
        for key, value in filters.items():
            if hasattr(DocumentRecord, key):
                column = getattr(DocumentRecord, key)
                if isinstance(value, (str, int, bool)):
                    doc_filter_conditions.append(column == value)
                elif key.endswith("_gte") and isinstance(value, datetime):
                    actual_key = key[:-4]
                    if hasattr(DocumentRecord, actual_key):
                        doc_filter_conditions.append(
                            getattr(DocumentRecord, actual_key) >= value
                        )
                elif key.endswith("_lte") and isinstance(value, datetime):
                    actual_key = key[:-4]
                    if hasattr(DocumentRecord, actual_key):
                        doc_filter_conditions.append(
                            getattr(DocumentRecord, actual_key) <= value
                        )
                # Add more filter handling here
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
    distance_op = DocumentEmbeddingRecord.embedding.cosine_distance
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
        .limit(limit * 5)
        .subquery("vector_subquery")
    )
    vector_results_cte = select(
        vector_subquery.c.embedding_id,
        vector_subquery.c.document_id,
        vector_subquery.c.distance,
        func.row_number()
        .over(order_by=vector_subquery.c.distance.asc())
        .label("vec_rank"),
    ).cte("vector_results")

    # --- 4. FTS Search CTE (Conditional) ---
    fts_results_cte = None
    if keywords:
        tsvector_col = func.to_tsvector("english", DocumentEmbeddingRecord.content)
        tsquery = func.plainto_tsquery("english", keywords)
        fts_subquery = (
            select(
                DocumentEmbeddingRecord.id.label("embedding_id"),
                DocumentEmbeddingRecord.document_id,
                func.ts_rank(tsvector_col, tsquery).label("score"),
            )
            .join(
                DocumentRecord, DocumentEmbeddingRecord.document_id == DocumentRecord.id
            )
            .where(doc_filter)
            .where(DocumentEmbeddingRecord.content != None)
            .where(tsvector_col.op("@@")(tsquery))
            .order_by(literal_column("score").desc())
            .limit(limit * 5)
            .subquery("fts_subquery")
        )
        fts_results_cte = select(
            fts_subquery.c.embedding_id,
            fts_subquery.c.document_id,
            fts_subquery.c.score,
            func.row_number()
            .over(order_by=fts_subquery.c.score.desc())
            .label("fts_rank"),
        ).cte("fts_results")

    # --- 5. Final Query Construction (remains the same logic) ---
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
    final_query = (
        select(*final_select_cols)
        .select_from(DocumentEmbeddingRecord)
        .join(DocumentRecord, DocumentEmbeddingRecord.document_id == DocumentRecord.id)
        .join(
            vector_results_cte,
            DocumentEmbeddingRecord.id == vector_results_cte.c.embedding_id,
            isouter=True,
        )
    )
    if fts_results_cte is not None:
        final_select_cols.extend(
            [fts_results_cte.c.score.label("fts_score"), fts_results_cte.c.fts_rank]
        )
        final_query = final_query.join(
            fts_results_cte,
            DocumentEmbeddingRecord.id == fts_results_cte.c.embedding_id,
            isouter=True,
        )
        rrf_score = (
            func.coalesce(1.0 / (60 + vector_results_cte.c.vec_rank), 0.0)
            + func.coalesce(1.0 / (60 + fts_results_cte.c.fts_rank), 0.0)
        ).label("rrf_score")
        final_select_cols.append(rrf_score)
        final_query = final_query.where(
            or_(
                vector_results_cte.c.embedding_id != None,
                fts_results_cte.c.embedding_id != None,
            )
        )
        final_query = final_query.order_by(rrf_score.desc())
    else:
        final_query = final_query.where(vector_results_cte.c.embedding_id != None)
        final_query = final_query.order_by(vector_results_cte.c.distance.asc())
    final_query = final_query.limit(limit)
    final_query = final_query.with_only_columns(*final_select_cols)

    # --- 6. Execute and Return using DatabaseContext ---
    try:
        logger.debug(f"Executing vector search query: {final_query}")
        # Use fetch_all which handles retry logic internally
        rows = await db_context.fetch_all(final_query)
        logger.info(f"Vector query returned {len(rows)} results.")
        # fetch_all already returns list of dict-like mappings
        return rows
    except SQLAlchemyError as e:
        logger.error(f"Database error during vector query: {e}", exc_info=True)
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
