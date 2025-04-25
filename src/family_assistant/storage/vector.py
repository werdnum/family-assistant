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
from sqlalchemy import JSON # Import generic JSON
from sqlalchemy.dialects.postgresql import JSONB  # Import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import functions  # Import functions explicitly

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
    doc_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON().with_variant(JSONB, 'postgresql')) # Use variant

    embeddings: Mapped[List["DocumentEmbeddingRecord"]] = sa.orm.relationship(
        "DocumentEmbeddingRecord",
        back_populates="document_record",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        sa.Index("idx_documents_doc_metadata", doc_metadata, postgresql_using="gin"), # Updated index definition
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
        # GIN expression index for FTS
        sa.Index(
            "idx_doc_embeddings_content_fts_gin",
            sa.func.to_tsvector(sa.literal_column("english"), content),
            postgresql_using="gin",
            postgresql_where=(content.isnot(None)),
        ),
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
    if engine.dialect.name == 'postgresql':
        logger.info("PostgreSQL dialect detected. Initializing vector extension and indexes...")
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
        except Exception as e:
            # This might happen if the index exists but with different params, etc.
            # In a real app, consider more robust migration management.
                logger.warning(
                    f"Could not create partial index idx_doc_embeddings_gemini_1536_hnsw_cos: {e}"
                )
        logger.info("PostgreSQL vector database components (extension, indexes) initialized.")
    else:
        logger.warning(
            f"Database dialect is '{engine.dialect.name}', not 'postgresql'. Skipping vector extension and index creation. Vector search functionality may be limited or unavailable."
        )


async def add_document(
    doc: Document,  # Use the renamed Protocol
    enriched_doc_metadata: Optional[ # Renamed parameter for clarity
        Dict[str, Any] # Renamed parameter for clarity
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
    """
    # TODO: Implement actual insert/conflict handling logic
    logger.info(f"Skeleton: Adding document record with source_id {doc.source_id}")
    # Example (needs proper async session handling and error checking):
    # async with async_sessionmaker(get_engine())() as session:
    #     async with session.begin():
    #         # Merge data from doc.metadata (protocol method) and the enriched parameter
    #         final_doc_metadata = {**(doc.metadata or {}), **(enriched_doc_metadata or {})}
    #         # Find existing or create new DocumentRecord row using doc properties
    #         # Store the merged data into the 'doc_metadata' database column
    #         # stmt = insert(DocumentRecord).values(..., doc_metadata=final_doc_metadata, ...).on_conflict_do_update(...) # Store in renamed column
    #         # result = await session.execute(stmt) # Store in renamed column
    #         # doc_id = result.inserted_primary_key[0] or fetch existing id
    #         pass
    return 1  # Placeholder


async def get_document_by_source_id(source_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves a document by its source ID."""
    # TODO: Implement actual select logic
    logger.info(f"Skeleton: Getting document by source_id {source_id}")
    return None  # Placeholder


async def add_embedding(
    document_id: int,
    chunk_index: int,
    embedding_type: str,
    embedding: List[float],
    embedding_model: str,
    content: Optional[str] = None,
    content_hash: Optional[str] = None,
):
    """Adds an embedding record linked to a document."""
    # TODO: Implement actual insert logic
    logger.info(
        f"Skeleton: Adding embedding for doc {document_id}, type {embedding_type}, model {embedding_model}"
    )
    pass


async def delete_document(document_id: int):
    """Deletes a document and its associated embeddings."""
    # TODO: Implement actual delete logic using cascade
    logger.info(f"Skeleton: Deleting document {document_id} and its embeddings")
    pass


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
        with its metadata and scores. Returns an empty list if skeleton.
    """
    # TODO: Implement the complex SQL query with RRF as shown in the design doc.
    #       This will involve constructing the WHERE clauses based on filters,
    #       using the correct vector dimension based on embedding_model,
    #       and performing the RRF calculation.
    logger.info(
        f"Skeleton: Querying vectors with model {embedding_model}. Keywords: {keywords}, Limit: {limit}"
    )
    logger.debug(f"Filters: {filters}, Embedding Types: {embedding_type_filter}")
    return []  # Placeholder


# Export functions explicitly for clarity when importing elsewhere
__all__ = [
    "init_vector_db",
    "add_document",
    "get_document_by_source_id",
    "add_embedding",
    "delete_document",
    "query_vectors",
    "DocumentRecord",  # Export SQLAlchemy model
    "DocumentEmbeddingRecord",  # Export SQLAlchemy model
    "Base",  # Export Base if needed for defining other models elsewhere (already exported)
    "Document",  # Export the protocol (formerly IngestibleDocument)
]
