"""
Processors for the document indexing pipeline.

This package contains various `ContentProcessor` implementations that form
the stages of the document indexing pipeline. Each processor is responsible
for a specific task, such as extracting text, generating summaries,
chunking content, or dispatching items for embedding.
"""

from .dispatch_processors import EmbeddingDispatchProcessor
from .file_processors import PDFTextExtractor
from .llm_processors import LLMIntelligenceProcessor
from .metadata_processors import TitleExtractor
from .network_processors import WebFetcherProcessor  # Added import
from .text_processors import TextChunker

__all__ = [
    "EmbeddingDispatchProcessor",
    "PDFTextExtractor",
    "LLMIntelligenceProcessor",
    "TitleExtractor",
    "TextChunker",
    "WebFetcherProcessor",  # Added to __all__
]
