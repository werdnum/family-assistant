"""
Content processors for handling specific file types.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

try:
    from markitdown import MarkItDown
except ImportError:
    MarkItDown = None  # type: ignore[assignment,misc] # misc for MarkItDown potentially not being a type

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
        if MarkItDown is None:  # Check for the class
            logger.warning(
                "markitdown library is not installed. PDFTextExtractor will not process PDFs. Passing through all items."
            )
            return current_items  # Pass through all items

        output_items: list[IndexableContent] = []
        # Instantiate the converter. If it's stateless, doing it per call is fine.
        # If it has significant setup cost and is stateless, consider instantiating it once per processor instance.
        md_converter = MarkItDown()

        for item in current_items:
            if item.mime_type == "application/pdf" and item.ref:
                logger.info(
                    f"PDFTextExtractor processing PDF: {item.ref} for document_id: {getattr(original_document, 'id', 'Unknown') if original_document else 'Unknown'}"
                )
                try:
                    # Run blocking markitdown conversion in a thread
                    # Use the convert method of the instantiated object
                    markdown_text_result = await asyncio.to_thread(
                        md_converter.convert, item.ref
                    )
                    # The convert method of MarkItDown (from scrape_mcp.py example) returns an object
                    # with a text_content attribute.
                    markdown_content = (
                        markdown_text_result.text_content
                        if markdown_text_result
                        else None
                    )

                    if markdown_content:
                        new_metadata = item.metadata.copy()
                        new_metadata["extraction_method"] = "markitdown"
                        # original_filename should be in item.metadata from DocumentIndexer

                        output_items.append(
                            IndexableContent(
                                content=markdown_content,
                                embedding_type="extracted_markdown_content",  # New type for raw markdown
                                mime_type="text/markdown",
                                source_processor=self.name,
                                metadata=new_metadata,
                                ref=None,  # Content is now inline
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
                # If PDF processing failed or resulted in no content, the original PDF item is consumed
                # and not added to output_items.
            else:
                # If the item is not a PDF to be processed by this stage, pass it through.
                output_items.append(item)

        return output_items
