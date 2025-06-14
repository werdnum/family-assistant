"""Repository for vector storage operations."""

from typing import Any, Protocol

from family_assistant.storage.repositories.base import BaseRepository


class Document(Protocol):
    """Protocol for document objects."""

    @property
    def source_id(self) -> str:
        """Unique identifier for the document source."""
        ...

    @property
    def source_type(self) -> str:
        """Type of the document source."""
        ...

    @property
    def title(self) -> str:
        """Document title."""
        ...

    @property
    def description(self) -> str | None:
        """Optional document description."""
        ...

    @property
    def created_at(self) -> Any:
        """Creation timestamp."""
        ...

    def to_dict(self) -> dict[str, Any]:
        """Convert document to dictionary."""
        ...


class VectorRepository(BaseRepository):
    """Repository for managing vector storage operations."""

    def __init__(self, db_context: Any) -> None:
        """Initialize the vector repository."""
        super().__init__(db_context)
        # Import here to avoid circular dependencies
        try:
            from family_assistant.storage import vector

            self._vector_module = vector
            self._enabled = True
        except ImportError:
            self._logger.warning("Vector storage module not available")
            self._vector_module = None
            self._enabled = False

    async def init_db(self) -> None:
        """Initialize vector database components."""
        if not self._enabled:
            self._logger.warning("Vector storage not enabled, skipping init")
            return

        try:
            await self._vector_module.init_vector_db(db_context=self._db)
            self._logger.info("Vector database initialized successfully")
        except Exception as e:
            self._logger.error(f"Failed to initialize vector database: {e}")
            raise

    async def add_document(
        self,
        doc: Document,
        enriched_doc_metadata: dict[str, Any] | None = None,
    ) -> int:
        """
        Add or update a document in the vector storage.

        Args:
            doc: Document object to store
            enriched_doc_metadata: Optional enriched metadata

        Returns:
            Document ID
        """
        if not self._enabled:
            self._logger.warning("Vector storage not enabled")
            return -1

        try:
            return await self._vector_module.add_document(
                db_context=self._db,
                doc=doc,
                enriched_doc_metadata=enriched_doc_metadata,
            )
        except Exception as e:
            self._logger.error(f"Failed to add document: {e}")
            raise

    async def get_document_by_id(self, document_id: int) -> dict[str, Any] | None:
        """
        Get a document by its ID.

        Args:
            document_id: The document ID

        Returns:
            Document data or None if not found
        """
        if not self._enabled:
            return None

        try:
            return await self._vector_module.get_document_by_id(
                db_context=self._db,
                document_id=document_id,
            )
        except Exception as e:
            self._logger.error(f"Failed to get document by ID {document_id}: {e}")
            raise

    async def get_document_by_source_id(self, source_id: str) -> dict[str, Any] | None:
        """
        Get a document by its source ID.

        Args:
            source_id: The source ID

        Returns:
            Document data or None if not found
        """
        if not self._enabled:
            return None

        try:
            return await self._vector_module.get_document_by_source_id(
                db_context=self._db,
                source_id=source_id,
            )
        except Exception as e:
            self._logger.error(f"Failed to get document by source ID {source_id}: {e}")
            raise

    async def delete_document(self, document_id: int) -> bool:
        """
        Delete a document and its embeddings.

        Args:
            document_id: The document ID to delete

        Returns:
            True if deleted, False otherwise
        """
        if not self._enabled:
            return False

        try:
            return await self._vector_module.delete_document(
                db_context=self._db,
                document_id=document_id,
            )
        except Exception as e:
            self._logger.error(f"Failed to delete document {document_id}: {e}")
            raise

    async def add_embedding(
        self,
        document_id: int,
        chunk_index: int,
        embedding_type: str,
        embedding: list[float],
        text_content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Add an embedding for a document.

        Args:
            document_id: The document ID
            chunk_index: Index of the chunk within the document
            embedding_type: Type of embedding
            embedding: The embedding vector
            text_content: Text content for the embedding
            metadata: Optional metadata
        """
        if not self._enabled:
            return

        try:
            await self._vector_module.add_embedding(
                db_context=self._db,
                document_id=document_id,
                chunk_index=chunk_index,
                embedding_type=embedding_type,
                embedding=embedding,
                text_content=text_content,
                metadata=metadata,
            )
        except Exception as e:
            self._logger.error(f"Failed to add embedding: {e}")
            raise

    async def query_vectors(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        similarity_threshold: float = 0.7,
        embedding_types: list[str] | None = None,
        source_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query vectors by similarity.

        Args:
            query_embedding: The query vector
            top_k: Maximum number of results
            similarity_threshold: Minimum similarity score
            embedding_types: Filter by embedding types
            source_types: Filter by source types

        Returns:
            List of matching documents with similarity scores
        """
        if not self._enabled:
            return []

        try:
            return await self._vector_module.query_vectors(
                db_context=self._db,
                query_embedding=query_embedding,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                embedding_types=embedding_types,
                source_types=source_types,
            )
        except Exception as e:
            self._logger.error(f"Failed to query vectors: {e}")
            raise
