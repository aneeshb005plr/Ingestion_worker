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
        Get the last non-empty, non-heading paragraph from result lines.
        This is the caption/description that precedes the table.
        Only returns plain text paragraphs — not headings, not table rows.
        """
        for line in reversed(result):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip headings — they are already in breadcrumb
            if stripped.startswith("#"):
                continue
            # Skip italic/bold table captions like *Table 4: ...*
            # These are post-table captions, not pre-table descriptions
            if stripped.startswith("*") and stripped.endswith("*"):
                continue
            # Skip lines that are already table-converted prose
            if " | " in stripped and ":" in stripped:
                continue
            # This is a plain paragraph — return it
            return stripped
        return ""

    def _remove_preceding_paragraph(self, result: list[str]) -> list[str]:
        """
        Remove the last non-empty paragraph from result.
        Called after we prepend it to the first table row.
        Must mirror _get_preceding_paragraph exactly:
          empty lines    → continue (skip)
          headings (#)   → continue (skip — heading is not the caption)
          italic (*...*) → continue (skip — post-table caption, keep)
          table prose    → continue (skip — already converted rows)
          plain text     → remove it and return
        """
        for i in range(len(result) - 1, -1, -1):
            stripped = result[i].strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue  # skip heading — caption is before it
            if stripped.startswith("*") and stripped.endswith("*"):
                continue  # skip italic post-table caption
            if " | " in stripped and ":" in stripped:
                continue  # skip already-converted table prose
            result[i] = ""
            return result
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
                # Prepend context to FIRST row only
                if idx == 0 and preceding_context:
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
