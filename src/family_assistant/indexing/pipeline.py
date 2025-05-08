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
            by any processor. Embedding tasks are dispatched by specialized
            processors within the pipeline, not by the pipeline orchestrator itself.
        """
        items_for_next_stage: List[IndexableContent] = [initial_content]

        for processor in self.processors:
            if not items_for_next_stage:  # No more items to process
                break

            # Each processor returns the list of items to be passed to the next one.
            # Specialized processors (e.g., for dispatching embeddings) will handle
            # task enqueuing internally using the provided context.
            items_for_next_stage = await processor.process(
                items_for_next_stage, original_document, initial_content, context
            )
        return items_for_next_stage # These are items that completed all pipeline stages.
