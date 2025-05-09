"""
Content processors focused on extracting or generating metadata-related IndexableContent.
"""

import logging

from family_assistant.indexing.pipeline import ContentProcessor, IndexableContent
from family_assistant.storage.vector import Document
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class TitleExtractor(ContentProcessor):
    """
    Extracts the title from the original document and creates an IndexableContent item for it.
    """

    @property
    def name(self) -> str:
        return "TitleExtractor"

    async def process(
        self,
        current_items: list[IndexableContent],
        original_document: Document,
        initial_content_ref: IndexableContent,
        context: ToolExecutionContext,
    ) -> list[IndexableContent]:
        """
        Checks for a title in the original document. If found, creates an
        IndexableContent item for the title. Passes through all current_items
        and adds the new title item.
        """
        output_items = list(current_items)  # Start with existing items

        if original_document.title and original_document.title.strip():
            title_content = original_document.title.strip()
            title_item = IndexableContent(
                embedding_type="title",
                source_processor=self.name,
                content=title_content,
                mime_type="text/plain",
                metadata={"source_title_length": len(title_content)},
            )
            output_items.append(title_item)
            logger.debug(
                f"Extracted title '{title_content}' for document ID {original_document.source_id if hasattr(original_document, 'source_id') else 'N/A'}"
            )
        else:
            logger.debug(
                f"No title found or title is empty for document ID {original_document.source_id if hasattr(original_document, 'source_id') else 'N/A'}"
            )

        return output_items
