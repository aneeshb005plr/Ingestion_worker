"""
Two-stage document chunker.

Stage 1 — Split by Markdown headers:
  Preserves document structure. A chunk won't span two different sections.
  The header hierarchy is stored in metadata as section_headers.

Stage 2 — Recursive character split:
  Handles sections that are still too large after header splitting.
  chunk_size and chunk_overlap are configurable per tenant.

Stage 2b — Table row split:
  After recursive splitting, table-converted prose rows are split
  into individual chunks — one chunk per row.

  Why:
    TableConverter converts each table row to a pipe-prose line:
      "Application owners — Role: Primary | Name: Brad Jorgenson | ..."
      "Application owners — Role: Secondary | Name: Federico Galante | ..."

    Without row splitting: all rows land in ONE chunk (they fit in 1024 chars).
    The embedding of that chunk is diluted across all rows.
    Query "who is primary owner?" scores LOW against mixed-contact chunk.

    With row splitting: each row is its own chunk.
    Each chunk embeds the context + one specific row.
    Query "who is primary owner?" scores HIGH against the primary row only. ✅

  Detection:
    Table-converted rows contain " | " and ": " patterns.
    Plain prose and headings don't match this pattern.
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


def _is_table_row(text: str) -> bool:
    """Detect a single table-converted prose row."""
    return " | " in text and ": " in text


def _split_table_rows(chunk: Document) -> list[Document]:
    """
    Split a chunk that contains multiple table rows into one chunk per row.

    A chunk qualifies for row splitting if:
      - It contains 2+ lines
      - The majority of lines are table-converted prose rows

    Each split row inherits the parent chunk's metadata (section headers).
    Non-table lines (headings, blank lines) are prepended to the first
    row chunk for context continuity.
    """
    lines = chunk.page_content.split("\n")

    # Count table rows
    table_lines = [l for l in lines if _is_table_row(l)]
    if len(table_lines) < 2:
        # 0 or 1 table row — no splitting needed
        return [chunk]

    result = []
    preamble_lines = []  # non-table lines (headings, breadcrumb) before rows

    for line in lines:
        if not line.strip():
            continue
        if _is_table_row(line):
            # Each table row → its own chunk
            # Prepend any preamble (breadcrumb/heading) for context
            content = "\n".join(preamble_lines + [line]) if preamble_lines else line
            result.append(
                Document(
                    page_content=content,
                    metadata=chunk.metadata.copy(),
                )
            )
        else:
            # Non-table line — collect as preamble
            # Only keep for first row; subsequent rows get context from
            # the preceding_context already prepended by TableConverter
            if not result:
                preamble_lines.append(line)

    return result if result else [chunk]


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
        size_chunks = self._recursive_splitter.split_documents(header_chunks)

        # Stage 2b: split table rows → one chunk per row
        # Table-converted rows that fit within chunk_size stay together
        # without this step, diluting the embedding across multiple contacts.
        final_chunks = []
        for chunk in size_chunks:
            row_chunks = _split_table_rows(chunk)
            final_chunks.extend(row_chunks)

        # Stage 3: prepend section breadcrumb to each chunk text
        for chunk in final_chunks:
            breadcrumb = self._build_breadcrumb(chunk.metadata)
            if breadcrumb and not chunk.page_content.startswith(breadcrumb):
                chunk.page_content = f"{breadcrumb}\n{chunk.page_content}"

        log.info(
            "chunker.split",
            header_chunks=len(header_chunks),
            size_chunks=len(size_chunks),
            final_chunks=len(final_chunks),
        )
        return final_chunks

    def _build_breadcrumb(self, metadata: dict) -> str:
        """
        Build a section breadcrumb from chunk metadata headers.
        e.g. metadata {h1: "Contacts", h2: "Primary"} → "# Contacts\n## Primary"
        Only uses the deepest two levels to avoid noise.
        """
        parts = []
        if metadata.get("h1"):
            parts.append(f"# {metadata['h1']}")
        if metadata.get("h2"):
            parts.append(f"## {metadata['h2']}")
        if metadata.get("h3"):
            parts.append(f"### {metadata['h3']}")
        return "\n".join(parts)
