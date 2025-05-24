"""
Content processors focused on extracting or generating metadata-related IndexableContent.
"""

import logging
from dataclasses import dataclass

from family_assistant.indexing.pipeline import ContentProcessor, IndexableContent
from family_assistant.storage.vector import Document, update_document_title_in_db
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Configuration for DocumentTitleUpdaterProcessor (can be extended later)
@dataclass
class DocumentTitleUpdaterProcessorConfig:
    """Configuration for the DocumentTitleUpdaterProcessor."""

    # Example: Prioritize title from metadata key 'fetched_title'
    title_metadata_key: str = "fetched_title"
    min_title_length: int = 3  # Minimum length for a title to be considered valid


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
        initial_content_ref: IndexableContent | None,
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


class DocumentTitleUpdaterProcessor(ContentProcessor):
    """
    Updates the main document's title in the database if a suitable title
    is found in the metadata of processed IndexableContent items (e.g., from WebFetcher).
    """

    def __init__(
        self, config: DocumentTitleUpdaterProcessorConfig | None = None
    ) -> None:
        self.config = config or DocumentTitleUpdaterProcessorConfig()

    @property
    def name(self) -> str:
        return "document_title_updater_processor"

    async def process(
        self,
        current_items: list[IndexableContent],
        original_document: Document,  # This should be the DocumentRecord instance
        initial_content_ref: IndexableContent | None,  # Not directly used here
        context: ToolExecutionContext,
    ) -> list[IndexableContent]:
        """
        Checks for a fetched title in item metadata and updates the document record.
        """
        if not hasattr(original_document, "id"):
            logger.error(
                f"[{self.name}] Original document is missing 'id' attribute. Cannot update title."
            )
            return current_items

        # We've confirmed 'id' attribute exists. Now get its value.
        # Use getattr to satisfy type checker for attribute access on a protocol
        # after a hasattr check. The type of id is expected to be int.
        doc_id_attr = original_document.id

        if not isinstance(doc_id_attr, int) or not doc_id_attr:  # Ensure it's a truthy integer
            logger.error(
                f"[{self.name}] Original document's 'id' attribute is not a truthy integer (found: {doc_id_attr!r}, type: {type(doc_id_attr).__name__}). Cannot update title."
            )
            return current_items

        # If we reach here, doc_id_attr is a truthy integer.
        document_id: int = doc_id_attr
        current_doc_title = (
            original_document.title
        )  # Get current title to potentially avoid overwriting good titles

        # Check if current title is already good enough (e.g. not a placeholder)
        # This logic can be expanded based on placeholder_prefixes in config
        # For now, we'll update if a fetched_title is found and is different.

        potential_title: str | None = None

        for item in current_items:
            if item.metadata and self.config.title_metadata_key in item.metadata:
                fetched_title_candidate = item.metadata[self.config.title_metadata_key]
                if (
                    isinstance(fetched_title_candidate, str)
                    and len(fetched_title_candidate.strip())
                    >= self.config.min_title_length
                ):
                    potential_title = fetched_title_candidate.strip()
                    logger.info(
                        f"[{self.name}] Found potential title '{potential_title}' from metadata key '{self.config.title_metadata_key}' for document ID {document_id}."
                    )
                    break  # Found a candidate, stop searching

        if potential_title and potential_title != current_doc_title:
            try:
                logger.info(
                    f"[{self.name}] Attempting to update title for document ID {document_id} from '{current_doc_title}' to '{potential_title}'."
                )
                await update_document_title_in_db(
                    db_context=context.db_context,
                    document_id=document_id,
                    new_title=potential_title,
                )
                # Note: The original_document object in memory won't reflect this change
                # unless re-fetched. This is generally fine as its primary use here is for its ID.
            except Exception as e:
                logger.error(
                    f"[{self.name}] Failed to update title for document ID {document_id}: {e}",
                    exc_info=True,
                )
        elif potential_title and potential_title == current_doc_title:
            logger.info(
                f"[{self.name}] Potential title '{potential_title}' is same as current for document ID {document_id}. No update needed."
            )
        else:
            logger.info(
                f"[{self.name}] No suitable new title found in metadata for document ID {document_id} or title is same as current. Current title: '{current_doc_title}'"
            )

        # This processor does not modify the list of items, only updates the DB.
        return current_items
