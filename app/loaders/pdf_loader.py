"""
PDFLoader — converts PDF documents to Markdown.

Two-stage approach:
  Stage 1 — pymupdf4llm (fast, offline, zero API calls)
             Handles: text PDFs with headings, paragraphs, simple tables
  Stage 2 — LangChain LLM enrichment (only when complexity detected)
             Triggers:
               - Scanned PDF (text layer empty/sparse) → Vision OCR per page
               - Images embedded in PDF → Vision descriptions
               - Complex tables detected → LLM cleanup

Complexity detection per-page before any LLM calls —
only pages that need enrichment incur API cost.

Corporate-safe: pymupdf is a pure C library, zero model downloads,
zero outbound calls in Stage 1. Stage 2 uses your existing approved
LangChain ChatOpenAI wrapper (langchain_openai.ChatOpenAI).
"""

import asyncio
import base64
import tempfile
from pathlib import Path

import structlog

from app.loaders.base import BaseLoader, ParsedDocument
from app.connectors.base import RawDocument
from app.core.exceptions import DocumentParsingError

log = structlog.get_logger(__name__)

# Thresholds
_SCANNED_TEXT_THRESHOLD = 50  # chars per page — below this = likely scanned
_MAX_PAGES_FOR_VISION = 30  # cap Vision calls for very large documents


class PDFLoader(BaseLoader):

    def __init__(self, llm=None) -> None:
        """
        Args:
            llm: Optional langchain_openai.ChatOpenAI instance.
                 Required for scanned PDFs and image enrichment.
                 If None, library-only mode — scanned PDFs will fail gracefully.
        """
        self._llm = llm
        log.info("loader.pdf.initialised", llm_enrichment=llm is not None)

    def supported_mime_types(self) -> list[str]:
        return ["application/pdf"]

    async def load(self, raw_doc: RawDocument) -> ParsedDocument:
        import pymupdf

        # ── Stage 1: Inspect PDF complexity ──────────────────────────────────
        try:
            pdf_doc = pymupdf.open(stream=raw_doc.content, filetype="pdf")
        except Exception as e:
            raise DocumentParsingError(
                f"Cannot open PDF '{raw_doc.file_name}': {e}"
            ) from e

        complexity = self._inspect_complexity(pdf_doc)

        log.debug(
            "loader.pdf.complexity",
            file=raw_doc.file_name,
            **{k: v for k, v in complexity.items() if k != "page_details"},
        )

        # ── Stage 2a: Scanned PDF — full Vision OCR ───────────────────────────
        if complexity["is_scanned"]:
            if self._llm is None:
                log.warning(
                    "loader.pdf.scanned_no_llm",
                    file=raw_doc.file_name,
                    message="Scanned PDF detected but LLM not configured — "
                    "text extraction will be empty or very sparse",
                )
            else:
                markdown_text = await self._ocr_via_vision(pdf_doc, raw_doc.file_name)
                pdf_doc.close()
                return ParsedDocument(
                    markdown_text=markdown_text,
                    extracted_metadata={
                        "loader": "pdf_vision_ocr",
                        "llm_enrichment": ["vision_ocr"],
                        **{k: v for k, v in complexity.items() if k != "page_details"},
                    },
                )

        # ── Stage 2b: Text PDF — pymupdf4llm extraction ───────────────────────
        def _extract_sync() -> str:
            import pymupdf4llm

            return pymupdf4llm.to_markdown(pdf_doc)

        try:
            markdown_text = await asyncio.to_thread(_extract_sync)
        except Exception as e:
            pdf_doc.close()
            raise DocumentParsingError(
                f"pymupdf4llm failed to parse '{raw_doc.file_name}': {e}"
            ) from e

        if not markdown_text.strip():
            pdf_doc.close()
            raise DocumentParsingError(
                f"No text extracted from '{raw_doc.file_name}' — "
                "document may be scanned or encrypted"
            )

        # ── Stage 3: Optional LLM enrichment for images ───────────────────────
        enrichment_applied = []

        if self._llm is not None and complexity["has_images"]:
            markdown_text = await self._enrich_images(
                pdf_doc, raw_doc.file_name, markdown_text
            )
            enrichment_applied.append("image_descriptions")

        pdf_doc.close()

        log.info(
            "loader.pdf.parsed",
            file=raw_doc.file_name,
            enrichment=enrichment_applied or "none",
            **{k: v for k, v in complexity.items() if k != "page_details"},
        )

        return ParsedDocument(
            markdown_text=markdown_text,
            extracted_metadata={
                "loader": "pymupdf4llm",
                "llm_enrichment": enrichment_applied,
                **{k: v for k, v in complexity.items() if k != "page_details"},
            },
        )

    # ── Complexity detection ──────────────────────────────────────────────────

    def _inspect_complexity(self, pdf_doc) -> dict:
        """
        Inspect PDF structure page by page.
        Cheap — uses pymupdf text/image metadata, no rendering.
        """
        total_pages = len(pdf_doc)
        total_chars = 0
        total_images = 0
        sparse_pages = 0  # pages with very little text
        page_details = []

        for page in pdf_doc:
            text = page.get_text()
            chars = len(text.strip())
            images = page.get_images()
            image_count = len(images)

            total_chars += chars
            total_images += image_count

            is_sparse = chars < _SCANNED_TEXT_THRESHOLD
            if is_sparse:
                sparse_pages += 1

            page_details.append(
                {
                    "page": page.number + 1,
                    "chars": chars,
                    "images": image_count,
                    "is_sparse": is_sparse,
                }
            )

        # Scanned = majority of pages have sparse/no text
        scanned_ratio = sparse_pages / total_pages if total_pages > 0 else 0
        is_scanned = scanned_ratio > 0.5

        return {
            "total_pages": total_pages,
            "total_chars": total_chars,
            "total_images": total_images,
            "sparse_pages": sparse_pages,
            "is_scanned": is_scanned,
            "has_images": total_images > 0,
            "needs_llm": is_scanned or total_images > 0,
            "page_details": page_details,
        }

    # ── LLM enrichment ────────────────────────────────────────────────────────

    async def _ocr_via_vision(self, pdf_doc, file_name: str) -> str:
        """
        Send each page as an image to GPT-4o Vision for OCR.
        Used only for scanned PDFs with no text layer.
        Capped at _MAX_PAGES_FOR_VISION to control cost.
        """
        from langchain_core.messages import HumanMessage

        total_pages = len(pdf_doc)
        pages_to_process = min(total_pages, _MAX_PAGES_FOR_VISION)

        if total_pages > _MAX_PAGES_FOR_VISION:
            log.warning(
                "loader.pdf.vision_page_cap",
                file=file_name,
                total_pages=total_pages,
                processing=pages_to_process,
                message=f"Large scanned PDF — processing first {pages_to_process} pages only",
            )

        log.info(
            "loader.pdf.ocr_started",
            file=file_name,
            pages=pages_to_process,
        )

        page_markdowns = []

        for page_num in range(pages_to_process):
            page = pdf_doc[page_num]

            # Render page to PNG image bytes (150 DPI — good balance quality/cost)
            mat = page.get_pixmap(dpi=150)
            image_bytes = mat.tobytes("png")
            image_b64 = base64.b64encode(image_bytes).decode()

            message = HumanMessage(
                content=[
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"This is page {page_num + 1} of {pages_to_process} from a scanned document. "
                            "Extract ALL text exactly as it appears, preserving structure. "
                            "Use Markdown: # for main headings, ## for sub-headings, "
                            "| col | col | for tables, - for bullet lists. "
                            "Include all text, numbers, and labels visible on the page. "
                            "Return only the Markdown content, no explanation."
                        ),
                    },
                ]
            )

            try:
                response = await self._llm.ainvoke([message])
                page_md = response.content.strip()
                page_markdowns.append(f"## Page {page_num + 1}\n\n{page_md}")
                log.debug("loader.pdf.page_ocr_done", file=file_name, page=page_num + 1)

            except Exception as e:
                log.warning(
                    "loader.pdf.page_ocr_failed",
                    file=file_name,
                    page=page_num + 1,
                    error=str(e),
                )
                page_markdowns.append(
                    f"## Page {page_num + 1}\n\n_Page could not be processed_"
                )

        if total_pages > _MAX_PAGES_FOR_VISION:
            page_markdowns.append(
                f"\n\n_Note: Document has {total_pages} pages. "
                f"Only first {_MAX_PAGES_FOR_VISION} pages were processed._"
            )

        return "\n\n---\n\n".join(page_markdowns)

    async def _enrich_images(
        self,
        pdf_doc,
        file_name: str,
        markdown_text: str,
    ) -> str:
        """
        Extract embedded images from a text PDF and add
        Vision descriptions appended to the Markdown.
        """
        from langchain_core.messages import HumanMessage

        descriptions = []

        for page_num in range(len(pdf_doc)):
            page = pdf_doc[page_num]
            image_list = page.get_images(full=True)

            for img_index, img_info in enumerate(image_list):
                try:
                    xref = img_info[0]
                    base_image = pdf_doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    image_ext = base_image["ext"]  # jpeg, png etc.
                    image_b64 = base64.b64encode(image_bytes).decode()

                    message = HumanMessage(
                        content=[
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/{image_ext};base64,{image_b64}",
                                    "detail": "high",
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Describe this image concisely for a RAG knowledge base. "
                                    "Focus on: key information, data shown, any text visible, "
                                    "charts/graphs, diagrams, and what the image communicates. "
                                    "Format as a single paragraph. Be specific and factual."
                                ),
                            },
                        ]
                    )

                    response = await self._llm.ainvoke([message])
                    description = response.content.strip()
                    descriptions.append(
                        f"\n\n**[Page {page_num + 1}, Image {img_index + 1}]**: {description}"
                    )

                except Exception as e:
                    log.warning(
                        "loader.pdf.image_description_failed",
                        file=file_name,
                        page=page_num + 1,
                        image=img_index + 1,
                        error=str(e),
                    )

        if descriptions:
            markdown_text += "\n\n## Embedded Images\n" + "".join(descriptions)

        return markdown_text
