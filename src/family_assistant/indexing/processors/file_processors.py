"""
Content processors for handling specific file types.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

try:
    import markitdown
except ImportError:
    markitdown = None  # type: ignore[assignment]

from family_assistant.indexing.pipeline import IndexableContent

if TYPE_CHECKING:
    from family_assistant.storage.vector import Document
    from family_assistant.tools.types import ToolExecutionContext


logger = logging.getLogger(__name__)


class PDFTextExtractor:
    """
    A content processor that extracts text from PDF files by converting them to Markdown
    using the markitdown library.
    """

    @property
    def name(self) -> str:
        """Unique identifier for the processor."""
        return "PDFTextExtractor"

    async def process(
        self,
        current_items: list[IndexableContent],
        original_document: "Document",  # noqa: ARG002
        initial_content_ref: IndexableContent | None,  # noqa: ARG002
        context: "ToolExecutionContext",  # noqa: ARG002
    ) -> list[IndexableContent]:
        """
        Processes IndexableContent items, converting PDFs to Markdown.

        Args:
            current_items: Content items from the previous stage.
            original_document: The Document object representing the source item.
            initial_content_ref: The very first IndexableContent item created for the document.
            context: Execution context.

        Returns:
            A list of new IndexableContent items containing extracted Markdown.
        """
        if markitdown is None:
            logger.warning(
                "markitdown library is not installed. PDFTextExtractor will not process PDFs."
            )
            return [] # Return empty list, effectively skipping PDF processing

        output_items: list[IndexableContent] = []

        for item in current_items:
            if item.mime_type == "application/pdf" and item.ref:
                logger.info(
                    f"PDFTextExtractor processing PDF: {item.ref} for document_id: {original_document.id if original_document else 'Unknown'}"
                )
                try:
                    # Run blocking markitdown conversion in a thread
                    markdown_content = await asyncio.to_thread(
                        markitdown.mdb.convert, item.ref
                    )

                    if markdown_content:
                        new_metadata = item.metadata.copy()
                        new_metadata["extraction_method"] = "markitdown"
                        # original_filename should be in item.metadata from DocumentIndexer

                        output_items.append(
                            IndexableContent(
                                content=markdown_content,
                                embedding_type="extracted_markdown_content", # New type for raw markdown
                                mime_type="text/markdown",
                                source_processor=self.name,
                                metadata=new_metadata,
                                ref=None, # Content is now inline
                            )
                        )
                        logger.info(
                            f"Successfully converted PDF '{item.metadata.get('original_filename', item.ref)}' to Markdown."
                        )
                    else:
                        logger.warning(
                            f"markitdown converted PDF '{item.metadata.get('original_filename', item.ref)}' to empty content."
                        )

                except Exception as e:
                    logger.error(
                        f"Error converting PDF '{item.metadata.get('original_filename', item.ref)}' with markitdown: {e}",
                        exc_info=True,
                    )
            # This processor only handles 'application/pdf'.
            # It consumes PDF items and produces new markdown items, or nothing if conversion fails.
            # Other item types are implicitly filtered out as they are not added to output_items.
            # The pipeline orchestrator passes the output of one processor (output_items from this one)
            # as input to the next.

        return output_items
