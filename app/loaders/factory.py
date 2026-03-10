"""
LoaderFactory — selects the right loader for any RawDocument.

Two registries:
  _file_registry       : mime_type → loader  (PDF, DOCX, text etc.)
  _structured_registry : source_type → loader (sql, mongodb)

Usage:
  factory.get_loader(raw_doc)  ← works for both file and structured

Adding a new file loader:
  1. Create loader class with supported_mime_types() returning MIME types
  2. Instantiate it in LoaderFactory.__init__ — auto-registers

Adding a new structured loader:
  1. Create loader class with supported_structured_types() returning ["sql"] etc.
  2. Instantiate it in LoaderFactory.__init__ — auto-registers
"""

import structlog

from app.loaders.base import BaseLoader
from app.loaders.text_loader import TextLoader
from app.loaders.pdf_loader import PDFLoader
from app.loaders.docx_loader import DocxLoader
from app.loaders.sql_loader import SQLLoader
from app.loaders.mongodb_loader import MongoDBLoader
from app.connectors.base import RawDocument
from app.core.exceptions import UnsupportedDocumentTypeError

log = structlog.get_logger(__name__)


class LoaderFactory:

    def __init__(self, llm=None) -> None:
        """
        Args:
            llm: Optional LangChain ChatModel for loader enrichment.
                 Passed to PDFLoader and DocxLoader.
                 Only invoked when complexity is detected — not for every document.
        """
        pdf = PDFLoader(llm=llm)
        docx = DocxLoader(llm=llm)
        text = TextLoader()
        sql = SQLLoader()
        mongodb = MongoDBLoader()

        # File registry: mime_type → loader
        self._file_registry: dict[str, BaseLoader] = {}
        for loader in [pdf, docx, text]:
            for mime_type in loader.supported_mime_types():
                self._file_registry[mime_type] = loader

        # Structured registry: source_type → loader
        self._structured_registry: dict[str, BaseLoader] = {}
        for loader in [sql, mongodb]:
            for source_type in loader.supported_structured_types():
                self._structured_registry[source_type] = loader

        log.info(
            "loader.factory.initialised",
            file_types=list(self._file_registry.keys()),
            structured_types=list(self._structured_registry.keys()),
        )

    def get_loader(self, raw_doc: RawDocument) -> BaseLoader:
        """
        Return the correct loader for a RawDocument.
        Works for both file and structured content types.

        Raises UnsupportedDocumentTypeError if no loader is registered.
        Caught per-document in ingestion_service — document marked skipped,
        job continues.
        """
        if raw_doc.is_file():
            mime_type = raw_doc.mime_type or ""
            loader = self._file_registry.get(mime_type)
            if loader is None:
                raise UnsupportedDocumentTypeError(
                    f"No loader for MIME type '{mime_type}' "
                    f"(file: {raw_doc.file_name}). Document skipped."
                )
            return loader

        else:
            # structured
            source_type = raw_doc.structured_source_type or ""
            loader = self._structured_registry.get(source_type)
            if loader is None:
                raise UnsupportedDocumentTypeError(
                    f"No loader for structured_source_type '{source_type}' "
                    f"(source: {raw_doc.file_name}). Document skipped."
                )
            return loader
