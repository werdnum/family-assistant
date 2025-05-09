"""
Core components for the document indexing pipeline.
Defines the structure of content flowing through the pipeline and the interface
for content processors, and the pipeline orchestrator.
"""

import logging  # Added
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from family_assistant.storage.vector import Document
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)  # Added


@dataclass
class IndexableContent:
    """
    Represents a unit of data flowing through the indexing pipeline,
    potentially ready for embedding.
    """

    embedding_type: str
    """Defines what the content represents (e.g., 'title', 'summary', 'content_chunk')."""

    source_processor: str
    """Name of the processor that generated this item."""

    content: str | None = None
    """The textual content (or None for binary references)."""

    mime_type: str | None = None
    """MIME type of the content (e.g., 'text/plain', 'image/jpeg')."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Processor-specific details (e.g., {'page_number': 3})."""

    ref: str | None = None
    """Reference to original binary data if content is None (e.g., temporary file path)."""


class ContentProcessor(Protocol):
    """
    Defines the contract for a stage in the document indexing pipeline.
    """

    @property
    def name(self) -> str:
        """Unique identifier for the processor."""
        ...

    async def process(
        self,
        current_items: list[IndexableContent],
        original_document: "Document",
        initial_content_ref: IndexableContent,
        context: "ToolExecutionContext",
    ) -> list[IndexableContent]:
        """
        Processes a list of IndexableContent items.

        Args:
            current_items: Content items from the previous stage or initial input.
            original_document: The Document object representing the source item.
            initial_content_ref: The very first IndexableContent item created for the document.
            context: Execution context providing access to database, task queue, etc.

        Returns:
            A list of IndexableContent items that need further processing
            by subsequent stages in the pipeline. Some processors (e.g., an
            EmbeddingDispatchProcessor) may also use the provided `context`
            to enqueue tasks, such as dispatching items for embedding.
        """
        ...


class IndexingPipeline:
    """
    Orchestrates the flow of IndexableContent through a series of ContentProcessors.
    """

    def __init__(
        self, processors: list[ContentProcessor], config: dict[str, Any]
    ) -> None:
        """
        Initializes the pipeline with a list of processors and a configuration.

        Args:
            processors: An ordered list of ContentProcessor instances.
            config: A dictionary for pipeline-level configuration.
                     (Note: Configuration passing to individual processors not yet detailed.)
        """
        self.processors = processors
        self.config = config  # Store config, usage TBD by specific pipeline needs

    async def run(
        self,
        initial_items: list[IndexableContent],  # Changed parameter name and type
        original_document: "Document",
        context: "ToolExecutionContext",
    ) -> list[IndexableContent]:
        """
        Runs the initial list of IndexableContent items through all configured processors sequentially.

        Args:
            initial_items: The first list of IndexableContent items to process. # Changed
            original_document: The source Document object.
            context: The execution context for processors.

        Returns:
            A list of IndexableContent items that have passed through all stages
            and may require further, non-pipeline processing, or represent the
            final state of content items that were not marked ready for embedding
            by any processor. Embedding tasks are dispatched by specialized
            processors within the pipeline, not by the pipeline orchestrator itself.
        """
        items_for_next_stage: list[IndexableContent] = (
            initial_items  # Changed initialization
        )

        for processor in self.processors:
            if not items_for_next_stage:  # No more items to process
                break

        if not self.processors:
            logger.warning(
                f"IndexingPipeline for document '{original_document.title if original_document else 'Unknown'}' has no processors configured. Returning initial items."
            )
            return items_for_next_stage

        # Determine the single "initial_content_ref" to be passed to all processors.
        # This should ideally be the very first IndexableContent created for the document.
        # If the pipeline starts with a list, we'll use the first item from that list if available.
        initial_content_ref_for_processors: IndexableContent | None = (
            initial_items[0] if initial_items else None
        )

        logger.info(
            f"Starting IndexingPipeline for document '{original_document.title if original_document else 'Unknown'}' with {len(items_for_next_stage)} initial item(s) and {len(self.processors)} processor(s)."
        )

        for processor in self.processors:
            if not items_for_next_stage:
                logger.info(
                    f"No items left to process before processor '{processor.name}' for document '{original_document.title if original_document else 'Unknown'}'. Ending pipeline early."
                )
                break

            logger.debug(
                f"Running processor '{processor.name}' with {len(items_for_next_stage)} item(s) for document '{original_document.title if original_document else 'Unknown'}'."
            )
            try:
                # Each processor returns the list of items to be passed to the next one.
                # Specialized processors (e.g., for dispatching embeddings) will handle
                # task enqueuing internally using the provided context.
                items_for_next_stage = await processor.process(
                    current_items=items_for_next_stage,
                    original_document=original_document,
                    initial_content_ref=initial_content_ref_for_processors,  # Pass determined ref
                    context=context,
                )
                logger.debug(
                    f"Processor '{processor.name}' produced {len(items_for_next_stage)} item(s) for the next stage for document '{original_document.title if original_document else 'Unknown'}'."
                )
            except Exception as e:
                logger.error(
                    f"Error in processor '{processor.name}' for document '{original_document.title if original_document else 'Unknown'}': {e}",
                    exc_info=True,
                )
                # Depending on desired error handling (e.g., continue with next processor or stop)
                # For now, re-raise to indicate a failure in this pipeline run for this document.
                raise RuntimeError(
                    f"Processor '{processor.name}' failed during execution for document '{original_document.title if original_document else 'Unknown'}'."
                ) from e

        logger.info(
            f"IndexingPipeline run completed for document '{original_document.title if original_document else 'Unknown'}'. "
            f"Returning {len(items_for_next_stage)} item(s) that completed all stages."
        )
        return items_for_next_stage
