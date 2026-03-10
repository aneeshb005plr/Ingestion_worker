"""TextLoader — handles plain text and Markdown files. No parsing needed."""

import structlog
from app.loaders.base import BaseLoader, ParsedDocument
from app.connectors.base import RawDocument

log = structlog.get_logger(__name__)


class TextLoader(BaseLoader):

    def supported_mime_types(self) -> list[str]:
        return ["text/plain", "text/markdown", "text/csv"]

    async def load(self, raw_doc: RawDocument) -> ParsedDocument:
        try:
            text = raw_doc.content.decode("utf-8", errors="replace")
        except Exception as e:
            log.warning(
                "loader.text.decode_error", file=raw_doc.file_name, error=str(e)
            )
            text = raw_doc.content.decode("latin-1", errors="replace")

        return ParsedDocument(
            markdown_text=text,
            extracted_metadata={"loader": "text"},
        )
