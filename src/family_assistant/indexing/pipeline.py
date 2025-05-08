"""
Core components for the document indexing pipeline.
Defines the structure of content flowing through the pipeline and the interface
for content processors, and the pipeline orchestrator.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.storage.vector import Document
    from family_assistant.tools.types import ToolExecutionContext


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

    content: Optional[str] = None
    """The textual content (or None for binary references)."""

    mime_type: Optional[str] = None
    """MIME type of the content (e.g., 'text/plain', 'image/jpeg')."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Processor-specific details (e.g., {'page_number': 3})."""

    ref: Optional[str] = None
    """Reference to original binary data if content is None (e.g., temporary file path)."""

    ready_for_embedding: bool = False
    """Flag indicating if this content item is ready for immediate embedding."""


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
        current_items: List[IndexableContent],
        original_document: "Document",
        initial_content_ref: IndexableContent,
        context: "ToolExecutionContext",
    ) -> List[IndexableContent]:
        """
        Processes a list of IndexableContent items.

        Args:
            current_items: Content items from the previous stage or initial input.
            original_document: The Document object representing the source item.
            initial_content_ref: The very first IndexableContent item created for the document.
            context: Execution context providing access to database, task queue, etc.

        Returns:
            A list of IndexableContent items that need further processing
            by subsequent stages in the pipeline. Processors are responsible for
            setting the `ready_for_embedding` flag on items they deem ready.
        """
        ...


class IndexingPipeline:
    """
    Orchestrates the flow of IndexableContent through a series of ContentProcessors.
    """

    def __init__(
        self, processors: List[ContentProcessor], config: Dict[str, Any]
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
        initial_content: IndexableContent,
        original_document: "Document",
        context: "ToolExecutionContext",
    ) -> List[IndexableContent]:
        """
        Runs the initial content through all configured processors sequentially.

        Args:
            initial_content: The first IndexableContent item to process.
            original_document: The source Document object.
            context: The execution context for processors.

        Returns:
            A list of IndexableContent items that have passed through all stages
            and may require further, non-pipeline processing, or represent the
            final state of content items that were not marked ready for embedding
            by any processor. The primary outcome of the pipeline's execution is
            the dispatching of `embed_and_store_batch` tasks by the pipeline itself
            for items marked `ready_for_embedding=True`.
        """
        items_for_next_stage: List[IndexableContent] = [initial_content]
        all_items_ready_for_embedding: List[IndexableContent] = []

        for processor in self.processors:
            if not items_for_next_stage:  # No more items to process
                break

            processed_items_from_current_stage = await processor.process(
                items_for_next_stage, original_document, initial_content, context
            )

            items_for_next_stage = []  # Reset for the next iteration
            for item in processed_items_from_current_stage:
                if item.ready_for_embedding:
                    all_items_ready_for_embedding.append(item)
                else:
                    items_for_next_stage.append(item)

        # After all processors, dispatch collected items ready for embedding.
        if all_items_ready_for_embedding:
            # This is where the IndexingPipeline would format and dispatch
            # the 'embed_and_store_batch' task.
            # For example (simplified, actual implementation needs more detail):
            # texts_to_embed = [item.content for item in all_items_ready_for_embedding if item.content]
            # metadata_list = [...] # Construct this based on item.embedding_type, item.metadata etc.
            # document_id = original_document.id # Assuming Document has an id
            #
            # if texts_to_embed and hasattr(original_document, 'id'):
            #     await context.enqueue_task("embed_and_store_batch", payload={...})
            # else:
            #     # Log warning or handle error if no content or document_id
            pass  # Placeholder for dispatch logic

        return items_for_next_stage # These are items that completed the pipeline and were not marked ready.
