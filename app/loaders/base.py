"""
Base loader abstraction.

Loaders transform raw bytes (RawDocument) into structured Markdown text.
The Markdown is then fed into the chunker.
"""

from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from app.connectors.base import RawDocument


class ParsedDocument(BaseModel):
    """
    Output of a loader — structured Markdown plus extracted metadata.

    markdown_text       : the full document as Markdown string
    extracted_metadata  : loader-specific fields (page_count, author etc.)
                          merged into chunk metadata during enrichment

    Pydantic ensures markdown_text is always a string and
    extracted_metadata is always a dict — no silent type coercion surprises.
    """

    markdown_text: str
    extracted_metadata: dict = Field(default_factory=dict)


class BaseLoader(ABC):
    """
    Contract for all document loaders.
    Input:  RawDocument (bytes + identity)
    Output: ParsedDocument (Markdown text + extracted metadata)
    """

    @abstractmethod
    async def load(self, raw_doc: RawDocument) -> ParsedDocument:
        """
        Parse raw document bytes into structured Markdown.
        Must be async — CPU-heavy loaders use asyncio.to_thread() internally.
        """
        pass

    @abstractmethod
    def supported_mime_types(self) -> list[str]:
        """Return list of MIME types this loader handles."""
        pass
