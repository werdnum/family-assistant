"""
Content processors focused on text manipulation, like chunking.
"""
import logging
from typing import List, Dict, Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

from family_assistant.indexing.pipeline import IndexableContent, ContentProcessor
from family_assistant.storage.vector import Document
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class TextChunker(ContentProcessor):
    """
    Splits textual content of IndexableContent items into smaller chunks.
    """

    DEFAULT_CHUNK_SIZE = 1000
    DEFAULT_CHUNK_OVERLAP = 200
    PROCESSED_MIME_TYPES = ["text/plain", "text/markdown", "text/html"] # Add more as needed

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            is_separator_regex=False,
        )

    @property
    def name(self) -> str:
        return "TextChunker"

    async def process(
        self,
        current_items: List[IndexableContent],
        original_document: Document,
        initial_content_ref: IndexableContent,
        context: ToolExecutionContext,
    ) -> List[IndexableContent]:
        output_items: List[IndexableContent] = []

        for item in current_items:
            if item.content and item.mime_type in self.PROCESSED_MIME_TYPES:
                if len(item.content) > 0: # Ensure content is not empty string
                    chunks = self._text_splitter.split_text(item.content)
                    for i, chunk_text in enumerate(chunks):
                        chunk_metadata = item.metadata.copy()
                        chunk_metadata.update({
                            "chunk_index": i,
                            "original_embedding_type": item.embedding_type,
                            "original_content_length": len(item.content),
                            "chunk_content_length": len(chunk_text),
                        })
                        chunk_item = IndexableContent(
                            embedding_type=f"{item.embedding_type}_chunk",
                            source_processor=self.name,
                            content=chunk_text,
                            mime_type=item.mime_type, # Retain original mime_type or set to text/plain
                            metadata=chunk_metadata,
                        )
                        output_items.append(chunk_item)
                    logger.debug(f"Split content from item (type: {item.embedding_type}) into {len(chunks)} chunks for document ID {original_document.source_id if hasattr(original_document, 'source_id') else 'N/A'}")
                else: # Empty content string
                    output_items.append(item) # Pass through if content is empty
            else: # Not text or not a processed mime type
                output_items.append(item) # Pass through non-text items or non-processed mime types
        return output_items

