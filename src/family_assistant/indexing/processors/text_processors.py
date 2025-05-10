"""
Content processors focused on text manipulation, like chunking.
"""

import logging
from collections.abc import Sequence

from family_assistant.indexing.pipeline import ContentProcessor, IndexableContent
from family_assistant.storage.vector import Document
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class TextChunker(ContentProcessor):
    """
    Splits textual content of IndexableContent items into smaller chunks.
    """

    DEFAULT_CHUNK_SIZE = 1000
    DEFAULT_CHUNK_OVERLAP = 200
    DEFAULT_SEPARATORS: Sequence[str] = ("\n\n", "\n", "\t", ". ", ", ", " ", "")
    PROCESSED_MIME_TYPES = [
        "text/plain",
        "text/markdown",
        "text/html",
    ]  # Add more as needed

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        separators: Sequence[str] | None = None,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        if (
            chunk_overlap >= chunk_size and chunk_size > 0
        ):  # Ensure overlap is less than chunk size
            logger.warning(
                f"Chunk overlap ({chunk_overlap}) is >= chunk size ({chunk_size}). Setting overlap to {chunk_size // 2}."
            )
            self.chunk_overlap = chunk_size // 2

        self._separators = (
            separators if separators is not None else self.DEFAULT_SEPARATORS
        )

    def _split_recursively(self, text: str, separators: Sequence[str]) -> list[str]:
        """Recursively tries to split text by the given separators."""
        if not text:
            return []

        if not separators:  # No more separators to try
            return [text]

        current_separator = separators[0]
        remaining_separators = separators[1:]

        # If the current separator is empty, it's the last resort (character-level split)
        # but we don't split by char here, just return the text for the merge step.
        if current_separator == "":
            return [text]

        try:
            splits = text.split(current_separator)
        except Exception:  # Fallback for unusual characters in separator
            splits = [text]  # Treat as unsplittable by this separator

        results = []
        for part in splits:
            if not part:  # Skip empty parts
                continue
            if (
                len(part) <= self.chunk_size / 2 and not remaining_separators
            ):  # Heuristic: if small enough and no more specific separators, keep.
                # Division by 2 is arbitrary, aims to keep small semantic units.
                results.append(part)
            else:  # Otherwise, if part is large or more separators exist, recurse
                results.extend(self._split_recursively(part, remaining_separators))
        return [r for r in results if r]  # Filter out any remaining empty strings

    def _chunk_text_natively(self, text: str) -> list[str]:
        """Combines recursive splitting with a final sliding window for overlap."""
        if not text:
            return []

        # Step 1: Recursive split to break down by semantic units
        initial_splits = self._split_recursively(text, self._separators)
        recombined_text = " ".join(s.strip() for s in initial_splits if s.strip())

        if not recombined_text:
            return []

        # Step 2: Apply sliding window with overlap
        final_chunks: list[str] = []
        text_len = len(recombined_text)
        current_pos = 0
        while current_pos < text_len:
            end_pos = min(current_pos + self.chunk_size, text_len)
            chunk = recombined_text[current_pos:end_pos]
            final_chunks.append(chunk)

            if end_pos == text_len:  # Reached the end
                break

            current_pos += self.chunk_size - self.chunk_overlap
            # Ensure progress if chunk_size is very close to chunk_overlap or if step is zero/negative
            if (
                current_pos >= end_pos
            ):  # If no progress or went backward due to large overlap
                current_pos = end_pos  # Force move to the end of the current chunk to ensure next one starts after.
                # This effectively means less or no overlap if step is too small.
        return [c for c in final_chunks if c.strip()]

    @property
    def name(self) -> str:
        return "TextChunker"

    async def process(
        self,
        current_items: list[IndexableContent],
        original_document: Document,
        initial_content_ref: IndexableContent,
        context: ToolExecutionContext,
    ) -> list[IndexableContent]:
        output_items: list[IndexableContent] = []

        for item in current_items:
            if item.content and item.mime_type in self.PROCESSED_MIME_TYPES:
                if len(item.content) > 0:  # Ensure content is not empty string
                    chunks = self._chunk_text_natively(item.content)
                    for i, chunk_text in enumerate(chunks):
                        chunk_metadata = item.metadata.copy()
                        chunk_metadata.update(
                            {
                                "chunk_index": i,
                                "original_embedding_type": item.embedding_type,
                                "original_content_length": len(item.content),
                                "chunk_content_length": len(chunk_text),
                            }
                        )
                        chunk_item = IndexableContent(
                            embedding_type=f"{item.embedding_type}_chunk",
                            source_processor=self.name,
                            content=chunk_text,
                            mime_type=item.mime_type,  # Retain original mime_type or set to text/plain
                            metadata=chunk_metadata,
                        )
                        output_items.append(chunk_item)
                    logger.debug(
                        f"Split content from item (type: {item.embedding_type}) into {len(chunks)} chunks for document ID {original_document.source_id if hasattr(original_document, 'source_id') else 'N/A'}"
                    )
                else:  # Empty content string
                    output_items.append(item)  # Pass through if content is empty
            else:  # Not text or not a processed mime type
                output_items.append(
                    item
                )  # Pass through non-text items or non-processed mime types
        return output_items
