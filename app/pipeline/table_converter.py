"""
TableConverter — converts Markdown pipe tables to prose sentences
and prepends the preceding paragraph to the first table row.

Why prepend preceding paragraph:
  Documents often have a caption/description paragraph immediately
  before a table. After chunking, this paragraph may end up in a
  different chunk from the table rows.

  Example:
    "Application owners and escalation contacts"   ← caption (has "owner")
    | Role | Name | Phone |
    | Primary | Brad Jorgenson | +1 214... |       ← table (no "owner")

  Without fix: query "who is the owner?" misses the table chunk
               because "owner" is NOT in the table rows
  With fix:    caption prepended → chunk has "owner" → scores high

  This is not duplication — it's context preservation. The caption
  IS part of the table's semantic meaning.

Why NOT wide tables (>4 cols):
  Wide data tables (changelog, numeric data) have no meaningful
  caption context that aids retrieval. They embed fine as-is.

Examples:
  Input:
    "Application owners and escalation contacts"
    | Role | Name | Phone |
    |---|---|---|
    | Primary | Brad Jorgenson | +1 214 |

  Output:
    Application owners and escalation contacts — Role: Primary | Name: Brad Jorgenson | Phone: +1 214
    Role: Secondary | Name: Federico Galante | Phone: +541...

  Input (wide data table — kept as-is):
    | Date | Version | Author | Change | Approved |
    → unchanged
"""

import re
import structlog

log = structlog.get_logger(__name__)

# Max columns to convert — wider tables are data tables, keep as-is
MAX_COLS_TO_CONVERT = 4


class TableConverter:

    def convert(self, markdown_text: str) -> str:
        """
        Convert key-value style pipe tables to prose.
        Prepends the immediately preceding paragraph to the first
        converted row so table context (caption/description) is preserved.
        Leaves wide data tables, code blocks, and non-table content unchanged.
        """
        lines = markdown_text.split("\n")
        result = []
        i = 0

        while i < len(lines):
            line = lines[i]

            if self._is_table_row(line):
                # Collect the full table block
                table_lines = []
                while i < len(lines) and (
                    self._is_table_row(lines[i]) or self._is_separator_row(lines[i])
                ):
                    table_lines.append(lines[i])
                    i += 1

                # Find preceding paragraph (context for this table)
                preceding = self._get_preceding_paragraph(result)

                converted = self._convert_table(
                    table_lines, preceding_context=preceding
                )

                # Remove the preceding paragraph from result — it will be
                # prepended to the first converted row instead
                if preceding and converted:
                    result = self._remove_preceding_paragraph(result)

                result.extend(converted)
            else:
                result.append(line)
                i += 1

        return "\n".join(result)

    def _is_table_row(self, line: str) -> bool:
        stripped = line.strip()
        return (
            stripped.startswith("|")
            and stripped.endswith("|")
            and "|" in stripped[1:-1]
        )

    def _is_separator_row(self, line: str) -> bool:
        stripped = line.strip()
        return bool(re.match(r"^\|[\s\-\|:]+\|$", stripped))

    def _parse_row(self, line: str) -> list[str]:
        cells = line.strip().strip("|").split("|")
        return [c.strip() for c in cells]

    def _get_preceding_paragraph(self, result: list[str]) -> str:
        """
        Get the caption/description that immediately precedes the table.

        Priority:
          1. Last plain text paragraph before the table (ideal)
          2. Last heading before the table (fallback when table immediately
             follows a heading with no caption paragraph between them)

        Why fall back to heading:
          Many tables have this structure:
            ### 3.2.1 List of components with brief description
            | Component | Technology | Function |    ← no plain text between

          Without fallback: preceding_context = "" → rows have no context
          With fallback: preceding_context = "3.2.1 List of components..."
          → each row embeds with meaningful context ✅
        """
        last_heading = ""
        for line in reversed(result):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip italic/bold table captions like *Table 4: ...*
            if stripped.startswith("*") and stripped.endswith("*"):
                continue
            # Skip lines that are already table-converted prose
            if " | " in stripped and ":" in stripped:
                continue
            # Heading — save as fallback but keep looking for plain text
            if stripped.startswith("#"):
                if not last_heading:
                    # Strip markdown # prefix to get clean text
                    last_heading = stripped.lstrip("#").strip()
                continue
            # Plain text paragraph — best option, return immediately
            return stripped

        # No plain text found — fall back to nearest heading
        return last_heading

    def _remove_preceding_paragraph(self, result: list[str]) -> list[str]:
        """
        Remove the preceding context line from result after prepending to table rows.
        Mirrors _get_preceding_paragraph priority exactly:
          1. Remove plain text paragraph if found
          2. Remove heading if it was used as fallback context

        Why remove the heading fallback:
          When heading is used as preceding_context, it gets prepended to
          every table row. Leaving it in result would cause duplication —
          the heading would appear both as a standalone line AND embedded
          in each row's context.

          Note: we only remove the heading if no plain text was found first,
          mirroring the exact fallback path of _get_preceding_paragraph.
        """
        last_heading_idx = None
        for i in range(len(result) - 1, -1, -1):
            stripped = result[i].strip()
            if not stripped:
                continue
            if stripped.startswith("*") and stripped.endswith("*"):
                continue
            if " | " in stripped and ":" in stripped:
                continue
            if stripped.startswith("#"):
                # Remember this heading index but keep looking for plain text
                if last_heading_idx is None:
                    last_heading_idx = i
                continue
            # Plain text found — remove it and return
            result[i] = ""
            return result

        # No plain text found — remove the heading that was used as fallback
        if last_heading_idx is not None:
            result[last_heading_idx] = ""
        return result

    def _convert_table(
        self,
        table_lines: list[str],
        preceding_context: str = "",
    ) -> list[str]:
        """
        Convert a table block to prose lines.
        Prepends preceding_context to the first row if provided.
        """
        rows = []
        header = None
        for line in table_lines:
            if self._is_separator_row(line):
                continue
            cells = self._parse_row(line)
            if not cells or all(c == "" for c in cells):
                continue
            if header is None:
                header = cells
            else:
                rows.append(cells)

        if not header:
            return table_lines

        num_cols = len(header)

        # Wide data tables — keep as-is
        if num_cols > MAX_COLS_TO_CONVERT:
            log.debug("table_converter.keeping_wide_table", cols=num_cols)
            return table_lines

        # Convert to prose
        output = []
        for idx, row in enumerate(rows):
            if not any(row):
                continue
            parts = []
            for col_idx, cell in enumerate(row):
                if col_idx < len(header) and cell:
                    head = header[col_idx]
                    if head:
                        parts.append(f"{head}: {cell}")
                    else:
                        parts.append(cell)
            if parts:
                prose_row = " | ".join(parts)
                # Prepend context to EVERY row — not just the first.
                #
                # Why every row:
                #   After table conversion, all rows land in a single chunk
                #   because they fit within chunk_size together.
                #   The embedding of that chunk is diluted across all rows.
                #
                #   Query "who is the primary owner?" against a chunk
                #   with 3 contacts scores LOW because the vector represents
                #   ALL contacts equally — no single person "wins".
                #
                #   With context on every row, the chunker can split them
                #   as individual self-contained units, each scoring HIGH
                #   for their specific content.
                #
                #   Even if chunker keeps them together, each row now carries
                #   its own context so the embedding is richer per row.
                if preceding_context:
                    prose_row = f"{preceding_context} — {prose_row}"
                output.append(prose_row)

        if output:
            log.debug(
                "table_converter.converted",
                cols=num_cols,
                rows=len(output),
                has_context=bool(preceding_context),
            )
            return output

        return table_lines
