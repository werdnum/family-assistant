"""
API for interacting with the vector storage database (PostgreSQL with pgvector).
Handles storing document metadata, text chunks, and their corresponding vector embeddings.
Provides functions for adding, retrieving, deleting, and querying documents and embeddings.

"""

import logging  # Import the logging module
from datetime import datetime
from typing import (
    Any,
    Protocol,
    cast,
)

import sqlalchemy as sa
from pgvector.sqlalchemy import (  # type: ignore[import-untyped]
    Vector,  # noqa F401 - Needs to be imported for SQLAlchemy type mapping
)
from sqlalchemy import (
    JSON,
    and_,
    delete,
    func,
    literal_column,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncResult, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, selectinload
from sqlalchemy.sql import functions  # Import functions explicitly

# Use absolute package path
from family_assistant.storage.base import metadata  # Keep metadata

# Remove get_engine import
from family_assistant.storage.context import DatabaseContext  # Import DatabaseContext

logger = logging.getLogger(__name__)


# --- Database Setup ---


# --- Protocol Definition ---
class Document(Protocol):
    (
        """Defines the interface for documents that can be ingested into vector storage."""
        """Defines the interface for document objects that can be ingested into vector storage."""
    )

    @property
    def source_type(self) -> str:
        """The type of the source (e.g., 'email', 'pdf', 'note')."""
        ...

    @property
    def source_id(self) -> str:
        """The unique identifier from the source system."""
        ...

    @property
    def source_uri(self) -> str | None:
        """URI or path to the original item, if applicable."""
        ...

    @property
    def title(self) -> str | None:
        """Title or subject of the document."""
        ...

    @property
    def created_at(self) -> datetime | None:
        """Original creation date of the item (must be timezone-aware if provided)."""
        ...

    @property
    def metadata(self) -> dict[str, Any] | None:
        """Base metadata extracted directly from the source (can be enriched later)."""
        ...


class Base(DeclarativeBase):
    # Associate metadata with this Base
    metadata = metadata


class DocumentRecord(Base):
    """SQLAlchemy model for the 'documents' table, representing stored document metadata."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)  # Changed to Integer
    source_type: Mapped[str] = mapped_column(sa.String(50), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(sa.Text)
    title: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), index=True
    )
    added_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=functions.now(),  # Use explicit import
    )  # Use sa.sql.func.now() for server default
    doc_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )  # Use variant

    embeddings: Mapped[list["DocumentEmbeddingRecord"]] = sa.orm.relationship(
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
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)  # Changed to Integer
    document_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,  # Changed to Integer
    )
    chunk_index: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    embedding_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    content: Mapped[str | None] = mapped_column(sa.Text)
    # Variable dimension vector requires pgvector >= 0.5.0
    embedding: Mapped[list[float]] = mapped_column(Vector, nullable=False)
    embedding_model: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(sa.Text)
    added_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=functions.now()
    )
    embedding_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )  # Renamed from metadata  # New metadata column

    document_record: Mapped["DocumentRecord"] = sa.orm.relationship(
        "DocumentRecord", back_populates="embeddings"
    )

    __table_args__ = (
        sa.UniqueConstraint("document_id", "chunk_index", "embedding_type"),
        sa.Index("idx_doc_embeddings_type_model", embedding_type, embedding_model),
        sa.Index("idx_doc_embeddings_document_chunk", document_id, chunk_index),
        # PostgreSQL-specific indexes (like HNSW or GIN on embedding/tsvector)
        # should be created conditionally elsewhere (e.g., init_vector_db), not defined here
        # to maintain compatibility with SQLite during metadata definition.
    )


# --- API Functions ---


async def init_vector_db(db_context: DatabaseContext) -> None:
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
    enriched_doc_metadata: dict[str, Any] | None = None,
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

    try:
        if db_context.engine.dialect.name != "postgresql":
            logger.info(
                "Non-PostgreSQL dialect detected for add_document. Using manual upsert logic."
            )
            # Manual upsert for SQLite and other non-PostgreSQL DBs
            # 1. Try to select existing document
            select_stmt = select(DocumentRecord.id).where(
                DocumentRecord.source_id == doc.source_id
            )
            existing_doc_row = await db_context.fetch_one(select_stmt)

            if existing_doc_row:
                doc_id = existing_doc_row["id"]
                # 2. If exists, update it
                update_stmt = (
                    sa.update(DocumentRecord)
                    .where(DocumentRecord.id == doc_id)
                    .values(**values_to_insert)
                )
                await db_context.execute_with_retry(update_stmt)
                logger.info(
                    f"Successfully updated document with source_id {doc.source_id}, ID: {doc_id}"
                )
            else:
                # 3. If not exists, insert it
                insert_stmt = (
                    insert(DocumentRecord).values(**values_to_insert)
                    # No .returning(DocumentRecord.id) for SQLite here
                )
                result = await db_context.execute_with_retry(insert_stmt)
                # For SQLite, get the last inserted ID via inserted_primary_key
                # Mypy might complain about inserted_primary_key, so add type: ignore if needed
                pk_tuple = result.inserted_primary_key  # type: ignore[attr-defined]
                if pk_tuple:
                    doc_id = pk_tuple[0]
                else:
                    # This would be unexpected if the insert succeeded and id is PK
                    logger.error(
                        f"Failed to retrieve inserted_primary_key for document with source_id {doc.source_id}"
                    )
                    raise RuntimeError(
                        "Failed to retrieve ID for newly inserted document."
                    )
                logger.info(
                    f"Successfully inserted new document with source_id {doc.source_id}, got ID: {doc_id}"
                )
            return doc_id
        else:
            # PostgreSQL: Use ON CONFLICT DO UPDATE
            stmt = insert(DocumentRecord).values(**values_to_insert)
            update_dict = {
                col: getattr(stmt.excluded, col)
                for col in values_to_insert
                if col != "source_id"  # Don't update the conflict target
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["source_id"],  # The unique constraint column
                set_=update_dict,
            ).returning(DocumentRecord.id)
            result = await db_context.execute_with_retry(stmt)
            doc_id_scalar = result.scalar_one()  # Get the inserted or existing ID
            if (
                doc_id_scalar is None
            ):  # Should not happen with scalar_one() but good for typing
                raise RuntimeError(f"Failed to get ID for document {doc.source_id}")
            doc_id = doc_id_scalar
            logger.info(
                f"Successfully added/updated document (PostgreSQL) with source_id {doc.source_id}, got ID: {doc_id}"
            )
            return doc_id
    except SQLAlchemyError as e:
        logger.error(
            f"Database error adding/updating document with source_id {doc.source_id}: {e}",
            exc_info=True,
        )
        raise


async def get_document_by_source_id(
    db_context: DatabaseContext,
    source_id: str,  # Added context
) -> DocumentRecord | None:
    """Retrieves a document ORM object by its source ID."""
    try:
        stmt = select(DocumentRecord).where(DocumentRecord.source_id == source_id)

        if db_context.conn is None:
            logger.error(
                "get_document_by_source_id called with a DatabaseContext that has no active connection."
            )
            raise RuntimeError("DatabaseContext has no active connection.")

        # Create an AsyncSession that will use the existing connection (and thus transaction)
        # from the db_context. The session does not own the connection or transaction lifecycle.
        async_session_instance = AsyncSession(
            bind=db_context.conn, expire_on_commit=False
        )
        try:
            # Execute the ORM statement using this session
            result = await async_session_instance.execute(stmt)
            record = result.scalar_one_or_none()
        finally:
            # Close the session; this does not close db_context.conn.
            await async_session_instance.close()

        if record:
            logger.debug(
                f"Found document with source_id {source_id} using db_context's transaction."
            )
            return record
        else:
            logger.debug(
                f"No document found with source_id {source_id} using db_context's transaction."
            )
            return None

    except SQLAlchemyError as e:  # Catch database-specific errors
        logger.error(
            f"Database error retrieving document with source_id {source_id}: {e}",
            exc_info=True,
        )
        raise
    except (
        Exception
    ) as e:  # Catch other potential errors like RuntimeError from pre-checks
        logger.error(
            f"Unexpected error retrieving document with source_id {source_id}: {e}",
            exc_info=True,
        )
        raise


async def get_document_by_id(
    db_context: DatabaseContext,
    document_id: int,  # Added context and id
) -> DocumentRecord | None:
    """Retrieves a document ORM object by its internal primary key ID."""
    try:
        stmt = (
            select(DocumentRecord)
            .where(DocumentRecord.id == document_id)
            .options(selectinload(DocumentRecord.embeddings))
        )

        if db_context.conn is None:
            logger.error(
                "get_document_by_id called with a DatabaseContext that has no active connection."
            )
            raise RuntimeError("DatabaseContext has no active connection.")

        # Create an AsyncSession that will use the existing connection (and thus transaction)
        # from the db_context. The session does not own the connection or transaction lifecycle.
        async_session_instance = AsyncSession(
            bind=db_context.conn, expire_on_commit=False
        )
        try:
            # Execute the ORM statement using this session
            result = await async_session_instance.execute(stmt)
            record = result.scalar_one_or_none()
        finally:
            # Close the session; this does not close db_context.conn.
            await async_session_instance.close()

        if record:
            logger.debug(
                f"Found document with ID {document_id} using db_context's transaction."
            )
            return record
        else:
            logger.debug(
                f"No document found with ID {document_id} using db_context's transaction."
            )
            return None
    except SQLAlchemyError as e:  # Catch database-specific errors
        logger.error(
            f"Database error retrieving document with ID {document_id}: {e}",
            exc_info=True,
        )
        raise
    except (
        Exception
    ) as e:  # Catch other potential errors like RuntimeError from pre-checks
        logger.error(
            f"Unexpected error retrieving document with ID {document_id}: {e}",
            exc_info=True,
        )
        raise


async def add_embedding(
    db_context: DatabaseContext,  # Added context
    document_id: int,
    chunk_index: int,
    embedding_type: str,
    embedding: list[float],
    embedding_model: str,
    content: str | None = None,
    content_hash: str | None = None,
    embedding_doc_metadata: dict[str, Any] | None = None,
) -> None:
    """
    Adds an embedding record linked to a document, updating if it already exists.

    Args:
        db_context: The DatabaseContext to use for the operation.
        document_id: ID of the parent document.
        chunk_index: Index of this chunk within the document for this embedding type.
        embedding_type: Type of embedding (e.g., 'title', 'content_chunk').
        embedding: The vector embedding.
        embedding_model: Name of the model used to generate the embedding.
        content: Optional textual content of the chunk.
        content_hash: Optional hash of the content.
        embedding_doc_metadata: Optional metadata specific to this embedding.
    """
    values_to_insert = {
        "document_id": document_id,
        "chunk_index": chunk_index,
        "embedding_type": embedding_type,
        "embedding": embedding,
        "embedding_model": embedding_model,
        "content": content,
        "content_hash": content_hash,
        "embedding_metadata": embedding_doc_metadata,
    }

    try:
        if db_context.engine.dialect.name != "postgresql":
            logger.info(
                "Non-PostgreSQL dialect detected for add_embedding. Using manual upsert logic."
            )
            # Manual upsert for SQLite and other non-PostgreSQL DBs
            # 1. Try to select existing embedding
            select_stmt = select(DocumentEmbeddingRecord.id).where(
                and_(
                    DocumentEmbeddingRecord.document_id == document_id,
                    DocumentEmbeddingRecord.chunk_index == chunk_index,
                    DocumentEmbeddingRecord.embedding_type == embedding_type,
                )
            )
            existing_embedding_row = await db_context.fetch_one(select_stmt)

            if existing_embedding_row:
                # 2. If exists, update it
                embedding_id = existing_embedding_row["id"]
                # Prepare update values, excluding primary key parts
                update_values_for_embedding = {
                    k: v
                    for k, v in values_to_insert.items()
                    if k not in ["document_id", "chunk_index", "embedding_type"]
                }
                update_stmt = (
                    sa.update(DocumentEmbeddingRecord)
                    .where(DocumentEmbeddingRecord.id == embedding_id)
                    .values(**update_values_for_embedding)
                )
                await db_context.execute_with_retry(update_stmt)
                logger.info(
                    f"Successfully updated embedding for doc {document_id}, chunk {chunk_index}, type {embedding_type}"
                )
            else:
                # 3. If not exists, insert it
                insert_stmt = insert(DocumentEmbeddingRecord).values(**values_to_insert)
                await db_context.execute_with_retry(insert_stmt)
                logger.info(
                    f"Successfully inserted new embedding for doc {document_id}, chunk {chunk_index}, type {embedding_type}"
                )
        else:
            # PostgreSQL: Use ON CONFLICT DO UPDATE
            stmt = insert(DocumentEmbeddingRecord).values(**values_to_insert)
            update_dict = {
                col: getattr(stmt.excluded, col)
                for col in values_to_insert
                if col not in ["document_id", "chunk_index", "embedding_type"]
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["document_id", "chunk_index", "embedding_type"],
                set_=update_dict,
            )
            await db_context.execute_with_retry(stmt)
            logger.info(
                f"Successfully added/updated embedding (PostgreSQL) for doc {document_id}, chunk {chunk_index}, type {embedding_type}"
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
    query_embedding: list[float],
    embedding_model: str,
    keywords: str | None = None,
    filters: dict[str, Any] | None = None,
    embedding_type_filter: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
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
    if filters is not None:  # Check if filters is not None before iterating
        for key, value in filters.items():
            if hasattr(DocumentRecord, key):
                # Ensure we are creating SQLAlchemy compatible expressions
                # For example, direct booleans are not allowed in .where()
                # This part seems okay as it uses column == value which is fine.
                # The issue might be if `doc_filter` itself becomes a Python bool.
                # However, `sa.true()` is used as a fallback, which is correct.
                # The error at L658 might be a misinterpretation or an older error.
                # We will focus on the `or_` clause error first.
                column = getattr(DocumentRecord, key)
                if isinstance(value, str | int | bool):
                    doc_filter_conditions.append(column == value)
                elif key.endswith("_gte") and isinstance(value, datetime):
                    actual_key = key[:-4]
                    if hasattr(DocumentRecord, actual_key):
                        doc_filter_conditions.append(
                            getattr(DocumentRecord, actual_key) >= value
                        )
                    else:
                        logger.warning(
                            f"Ignoring filter with unsupported key: {key} = {value}"
                        )
                elif key.endswith("_lte") and isinstance(value, datetime):
                    actual_key = key[:-4]
                    if hasattr(DocumentRecord, actual_key):
                        doc_filter_conditions.append(
                            getattr(DocumentRecord, actual_key) <= value
                        )
                    else:
                        logger.warning(
                            f"Ignoring filter with unsupported key: {key} = {value}"
                        )
                else:
                    logger.warning(
                        f"Ignoring filter with unsupported type or format: {key} = {value}"
                    )
                # Add more filter handling here
            else:
                logger.warning(f"Ignoring unknown filter key: {key}")
    doc_filter_expression: sa.sql.ColumnElement[bool] = (
        and_(*doc_filter_conditions) if doc_filter_conditions else sa.true()
    )

    # --- 2. Build Embedding Filter ---
    embedding_filter_conditions = [
        DocumentEmbeddingRecord.embedding_model == embedding_model
    ]
    if embedding_type_filter:
        embedding_filter_conditions.append(
            DocumentEmbeddingRecord.embedding_type.in_(embedding_type_filter)
        )
    embedding_filter_expression: sa.sql.ColumnElement[bool] = and_(
        *embedding_filter_conditions
    )

    logger.info("Filter for vector query: %s", doc_filter_expression)

    # --- 3. Vector Search CTE ---
    distance_op = DocumentEmbeddingRecord.embedding.cosine_distance
    vector_subquery = (
        select(
            DocumentEmbeddingRecord.id.label("embedding_id"),
            DocumentEmbeddingRecord.document_id,
            distance_op(query_embedding).label("distance"),
        )
        .join(DocumentRecord, DocumentEmbeddingRecord.document_id == DocumentRecord.id)
        .where(doc_filter_expression)
        .where(embedding_filter_expression)
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
            .where(doc_filter_expression)
            .where(DocumentEmbeddingRecord.content.is_not(None))  # Use .is_not(None)
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
        DocumentEmbeddingRecord.embedding_metadata,  # Add embedding_metadata to output
        DocumentEmbeddingRecord.chunk_index,
        vector_results_cte.c.distance,
        vector_results_cte.c.vec_rank,
    ]
    final_query_select = (  # Renamed to avoid conflict with final_query variable later
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
        final_select_cols.extend([
            fts_results_cte.c.score.label("fts_score"),
            fts_results_cte.c.fts_rank,
        ])
        final_query_select = final_query_select.join(
            fts_results_cte,
            DocumentEmbeddingRecord.id == fts_results_cte.c.embedding_id,
            isouter=True,
        )
        rrf_score = (
            func.coalesce(1.0 / (60 + vector_results_cte.c.vec_rank), 0.0)
            + func.coalesce(1.0 / (60 + fts_results_cte.c.fts_rank), 0.0)
        ).label("rrf_score")
        final_select_cols.append(rrf_score)
        final_query_select = final_query_select.where(
            or_(
                vector_results_cte.c.embedding_id.is_not(None),  # Use .is_not(None)
                fts_results_cte.c.embedding_id.is_not(None),  # Use .is_not(None)
            )
        )
        final_query_select = final_query_select.where(
            doc_filter_expression
        )  # Explicitly apply doc_filter
        final_query_select = final_query_select.order_by(rrf_score.desc())
    else:
        final_query_select = final_query_select.where(
            vector_results_cte.c.embedding_id.is_not(None)
        )  # Use .is_not(None)
        final_query_select = final_query_select.where(
            doc_filter_expression
        )  # Explicitly apply doc_filter
        final_query_select = final_query_select.order_by(
            vector_results_cte.c.distance.asc()
        )
    final_query_select = final_query_select.limit(limit)
    final_query = final_query_select.with_only_columns(
        *final_select_cols
    )  # Assign to final_query

    # --- 6. Execute and Return using DatabaseContext ---
    logger.info("Final vector query: %s", final_query)
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


async def update_document_title_in_db(
    db_context: DatabaseContext, document_id: int, new_title: str
) -> None:
    """
    Updates the title of a specific document in the database.

    Args:
        db_context: The DatabaseContext to use for the operation.
        document_id: The ID of the document to update.
        new_title: The new title for the document.
    """
    if not new_title or not new_title.strip():
        logger.warning(
            f"Attempted to update document {document_id} with an empty title. Skipping."
        )
        return

    stmt = (
        sa.update(DocumentRecord)
        .where(DocumentRecord.id == document_id)
        .values(title=new_title.strip())
    )
    try:
        result = await db_context.execute_with_retry(stmt)
        if cast("AsyncResult", result).rowcount > 0:
            logger.info(
                f"Successfully updated title for document ID {document_id} to '{new_title.strip()}'."
            )
        else:
            logger.warning(
                f"No document found with ID {document_id} to update title, or title was already the same."
            )
    except SQLAlchemyError as e:
        logger.error(
            f"Database error updating title for document ID {document_id}: {e}",
            exc_info=True,
        )
        raise  # Re-raise to allow task retry or failure handling


# Export functions explicitly for clarity when importing elsewhere
__all__ = [
    "init_vector_db",
    "update_document_title_in_db",  # Add new function to __all__
    "add_document",
    "get_document_by_source_id",
    "get_document_by_id",
    "add_embedding",
    "delete_document",
    "query_vectors",
    "DocumentRecord",  # Export SQLAlchemy ORM model
    "DocumentEmbeddingRecord",  # Export SQLAlchemy ORM model
    "Document",  # Export the protocol
]
