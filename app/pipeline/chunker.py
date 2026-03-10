"""
Two-stage document chunker.

Stage 1 — Split by Markdown headers:
  Preserves document structure. A chunk won't span two different sections.
  The header hierarchy is stored in metadata as section_headers.

Stage 2 — Recursive character split:
  Handles sections that are still too large after header splitting.
  chunk_size and chunk_overlap are configurable per tenant.
"""

import structlog
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from langchain_core.documents import Document

log = structlog.get_logger(__name__)

# Headers we split on — stored in chunk metadata as breadcrumb
_HEADERS_TO_SPLIT_ON = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
    ("####", "h4"),
]


class Chunker:

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 150) -> None:
        self._md_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=_HEADERS_TO_SPLIT_ON,
            strip_headers=False,  # keep header text in chunk for context
        )
        self._recursive_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            add_start_index=True,
        )

    def split(self, markdown_text: str) -> list[Document]:
        """
        Split markdown text into chunks.
        Returns list of LangChain Document objects with metadata.
        """
        if not markdown_text.strip():
            return []

        # Stage 1: split by headers → preserves structure
        header_chunks = self._md_splitter.split_text(markdown_text)

        # Stage 2: apply size limit → handles oversized sections
        final_chunks = self._recursive_splitter.split_documents(header_chunks)

        log.info(
            "chunker.split",
            header_chunks=len(header_chunks),
            final_chunks=len(final_chunks),
        )
        return final_chunks
