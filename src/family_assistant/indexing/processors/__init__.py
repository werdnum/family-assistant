"""
Processors for the document indexing pipeline.

This package contains various `ContentProcessor` implementations that form
the stages of the document indexing pipeline. Each processor is responsible
for a specific task, such as extracting text, generating summaries,
chunking content, or dispatching items for embedding.
"""

from family_assistant.indexing.processors.dispatch_processors import (
    EmbeddingDispatchProcessor,
)
from family_assistant.indexing.processors.file_processors import PDFTextExtractor
from family_assistant.indexing.processors.llm_processors import LLMIntelligenceProcessor
from family_assistant.indexing.processors.metadata_processors import TitleExtractor
from family_assistant.indexing.processors.network_processors import (
    WebFetcherProcessor,  # Added import
)
from family_assistant.indexing.processors.text_processors import TextChunker

__all__ = [
    "EmbeddingDispatchProcessor",
    "PDFTextExtractor",
    "LLMIntelligenceProcessor",
    "TitleExtractor",
    "TextChunker",
    "WebFetcherProcessor",  # Added to __all__
]
