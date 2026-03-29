"""
ChunkContextualiser — prepends LLM-generated context to every chunk.

Based on Anthropic's Contextual Retrieval research (Sep 2024):
  "Contextual Retrieval reduces failed retrievals by 49%, and 67%
   when combined with reranking."

Problem it solves:
  When documents are split into chunks, individual chunks lose
  their broader context. A chunk containing:
    "Primary / Secondary: Primary | Contact Name: Brad Jorgenson"
  has no semantic connection to "owner" because that word only
  appeared in the caption "Application owners and escalation contacts"
  which may be in a different chunk.

Solution:
  Before embedding, call a cheap LLM (e.g. gpt-4.1-mini) with:
    - The chunk text
    - The document title
    - The section headings (from chunk metadata)
    - The full document text (for full context)

  LLM returns a 2-sentence contextual summary.
  Prepend it to the chunk text before embedding.

R6b changes:
  - Prompt updated to use ONLY visible window context (no hallucination)
  - Abbreviation expansion removed (too risky for domain-specific terms)
  - Structural context emphasis: section, heading, entity identification
  - Works alongside R6a (document prefix) — complementary, not overlapping

Result:
  The contact chunk now reads:
    "This section lists the primary and secondary application owners
     and escalation contacts for Smart Pricing Tool, including names,
     phone numbers, and availability windows.

     Primary / Secondary: Primary | Contact Name: Brad Jorgenson..."

  "Who is the owner?" → scores 0.94 → always top 3 ✅

Modes:
  all_chunks   — every chunk enriched (best accuracy, recommended)
  tables_only  — only table-derived chunks enriched (cheaper)
                 Detected by: contains " | " and ": " patterns

Concurrency:
  contextualisation_semaphore limits concurrent LLM calls
  Independent from embed_semaphore — different rate limit pool
  Recommended: 3-5 concurrent calls (cheap model, fast)

Cost estimate (gpt-4.1-mini):
  ~300 tokens per chunk (doc context + chunk text + output)
  69 chunk SPT doc: ~20k tokens total
  Cost: ~$0.001 per document — negligible
  Paid once at ingestion, not at query time.

Failure handling:
  LLM failure on a chunk → keep original chunk unchanged
  Never fail the whole document because of contextualisation
  Logs warning with chunk index for debugging
"""

import asyncio
import structlog
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You are a document indexing assistant. Your task is to write a short contextual \
description of a text chunk so it can be accurately retrieved when users search \
for it using natural language questions.

Rules:
- Write exactly 2 sentences maximum
- Use ONLY information visible in the surrounding context window
- Focus on WHAT THE CHUNK CONTAINS, not WHERE it appears in the document
- Extract the key facts, values, names, technologies, decisions — not section numbers
- Use natural language that matches how users ask questions
- Use common synonyms: "owner"/"primary contact", "support"/"escalation", \
"technology"/"tech stack"/"framework", "config"/"settings", "steps"/"procedure"
- Do NOT expand abbreviations — keep them exactly as written in the document
- Do NOT infer or add information not present in the visible window
- Do NOT start with "This chunk", "This section", "This document", "This table"
- Do NOT describe structure — extract meaning
- Return ONLY the 2 sentences, nothing else
"""

CHUNK_PROMPT = """\
Document: {file_name}
Section: {section_path}

<context_window>
{window_context}
</context_window>

Chunk to describe:
<chunk>
{chunk_text}
</chunk>

The <context_window> above shows surrounding chunks from the SAME document.
Using ONLY what is visible in the context window, write 2 sentences that \
capture the MEANING and KEY FACTS of this chunk.

Focus on:
- WHO or WHAT the chunk is about (names, systems, technologies, roles)
- WHAT KEY FACTS it contains (values, decisions, configurations, contacts)
- WHAT QUESTION a user would ask to find this specific chunk

AVOID:
- Describing section numbers or document structure
- Saying "this section belongs to..."
- Mentioning page numbers or heading hierarchy

Examples:

Table with technologies:
  BAD:  "This section belongs to 3.2.1 List of components with brief description."
  GOOD: "SPT application uses NodeJS for microservices, MongoDB for the database, \
and Angular for the frontend. Users might ask what technology or tech stack the \
SPT application is built on."

Table with contacts:
  BAD:  "This section lists application owners and escalation contacts."
  GOOD: "Brad Jorgenson and Sheetal Shenoy are the primary application owners of \
Smart Pricing Tool reachable at +1 214-984-9828 during 8am-5pm EST. Users might \
ask who is the primary owner or contact for SPT."

Prose about deployment:
  BAD:  "This chunk belongs to the deployment section of the runbook."
  GOOD: "SPT is deployed on Azure Kubernetes Service using Docker images from \
artifacts-west.pwc.com. Users might ask how or where SPT is deployed."

Keep all abbreviations exactly as written.\
"""


class ChunkContextualiser:
    """
    Enriches chunks with LLM-generated contextual summaries before embedding.

    Usage:
        contextualiser = ChunkContextualiser(llm=task_loader_llm)
        enriched_chunks = await contextualiser.enrich(
            chunks=lc_chunks,
            file_name=raw_doc.file_name,
            mode="all_chunks",
            semaphore=contextualise_semaphore,
        )
    """

    def __init__(self, llm) -> None:
        """
        Args:
            llm: LangChain ChatModel instance (e.g. ChatOpenAI with gpt-4.1-mini)
                 Must support ainvoke().
        """
        self._llm = llm

    async def enrich(
        self,
        chunks: list[Document],
        file_name: str,
        mode: str = "all_chunks",
        window_size: int = 2,
        semaphore: asyncio.Semaphore | None = None,
    ) -> list[Document]:
        """
        Enrich chunks with LLM-generated contextual summaries.

        Uses a sliding window of surrounding chunks as context for the LLM.
        Processes chunks concurrently, bounded by semaphore.
        Failed chunks are returned unchanged — never raises.

        Args:
            chunks:      List of LangChain Document chunks from Chunker
            file_name:   Source file name (e.g. "SPT_Runbook.docx")
            mode:        "all_chunks" or "tables_only"
            window_size: Number of chunks before AND after to include
                         as context window. Default 2 = ±2 chunks.
                         Catches captions, headings, preceding paragraphs.
            semaphore:   Optional semaphore to limit concurrent LLM calls

        Returns:
            Same list with page_content prepended with context summary.
            Chunks that failed contextualisation are unchanged.
        """
        if not chunks:
            return chunks

        if semaphore is None:
            semaphore = asyncio.Semaphore(5)

        # Snapshot original content BEFORE concurrent mutation
        # Critical: asyncio.gather runs tasks concurrently — chunk N may be
        # mutated (enriched) while chunk N+2 is still reading it as window context.
        # We snapshot all original texts first so the window always sees
        # the ORIGINAL chunk text, not an already-enriched version.
        original_contents = [c.page_content for c in chunks]

        # Build read-only snapshot Documents for window context
        # These are never mutated — only original_chunks is passed to window
        snapshot_chunks = [
            Document(page_content=text, metadata=chunk.metadata)
            for text, chunk in zip(original_contents, chunks)
        ]

        tasks = [
            self._enrich_chunk(
                chunk=chunk,
                chunk_index=i,
                all_chunks=snapshot_chunks,  # ← original snapshots, never mutated
                window_size=window_size,
                file_name=file_name,
                mode=mode,
                semaphore=semaphore,
            )
            for i, chunk in enumerate(chunks)
        ]

        enriched = await asyncio.gather(*tasks)

        enriched_count = sum(
            1
            for orig_text, enr in zip(original_contents, enriched)
            if enr.page_content != orig_text
        )

        log.info(
            "contextualiser.enriched",
            file=file_name,
            total_chunks=len(chunks),
            enriched_chunks=enriched_count,
            mode=mode,
        )

        return list(enriched)

    async def _enrich_chunk(
        self,
        chunk: Document,
        chunk_index: int,
        all_chunks: list[Document],
        window_size: int,
        file_name: str,
        mode: str,
        semaphore: asyncio.Semaphore,
    ) -> Document:
        """
        Enrich a single chunk using surrounding chunks as context window.
        Returns unchanged chunk on any failure.
        """
        # Mode filter — skip non-table chunks if tables_only
        if mode == "tables_only" and not self._is_table_chunk(chunk.page_content):
            return chunk

        async with semaphore:
            try:
                context = await self._generate_context(
                    chunk_text=chunk.page_content,
                    chunk_index=chunk_index,
                    all_chunks=all_chunks,
                    window_size=window_size,
                    file_name=file_name,
                    metadata=chunk.metadata,
                )

                if context:
                    # Prepend context + blank line before original text
                    chunk.page_content = f"{context}\n\n{chunk.page_content}"
                    log.debug(
                        "contextualiser.chunk_enriched",
                        file=file_name,
                        chunk_index=chunk_index,
                        context_preview=context[:80],
                    )

            except Exception as e:
                # Never fail the document — log and continue with original chunk
                log.warning(
                    "contextualiser.chunk_failed",
                    file=file_name,
                    chunk_index=chunk_index,
                    error=str(e),
                )

        return chunk

    async def _generate_context(
        self,
        chunk_text: str,
        chunk_index: int,
        all_chunks: list[Document],
        window_size: int,
        file_name: str,
        metadata: dict,
    ) -> str:
        """
        Call LLM to generate a 2-sentence contextual summary.
        Uses sliding window of surrounding chunks as context.
        Returns empty string if LLM returns nothing useful.
        """
        # Build section path from chunk metadata breadcrumb
        section_parts = []
        for key in ("h1", "h2", "h3", "h4"):
            val = metadata.get(key)
            if val:
                section_parts.append(val)
        section_path = " > ".join(section_parts) if section_parts else "Document body"

        # Build sliding window — chunks before and after current chunk
        # window_size=2 → up to 2 chunks before + 2 after
        # This captures: preceding caption, section heading, following context
        start = max(0, chunk_index - window_size)
        end = min(len(all_chunks), chunk_index + window_size + 1)
        window_chunks = all_chunks[start:end]

        # Build window context — mark current chunk clearly
        window_parts = []
        for i, wc in enumerate(window_chunks):
            abs_index = start + i
            if abs_index == chunk_index:
                window_parts.append(f"[CURRENT CHUNK]\n{wc.page_content[:800]}")
            else:
                label = "before" if abs_index < chunk_index else "after"
                window_parts.append(f"[chunk {label}]\n{wc.page_content[:400]}")
        window_context = "\n\n---\n\n".join(window_parts)

        prompt = CHUNK_PROMPT.format(
            file_name=file_name,
            section_path=section_path,
            window_context=window_context,
            chunk_text=chunk_text[:1500],
        )

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        response = await self._llm.ainvoke(messages)
        context = response.content.strip()

        # Sanity check — must be non-empty and reasonable length
        if not context or len(context) < 20:
            return ""

        # Cap at 500 chars — we don't want context longer than the chunk
        return context[:500]

    @staticmethod
    def _is_table_chunk(text: str) -> bool:
        """
        Detect if a chunk originated from a table conversion.
        Table-converted chunks have " | " separators and ": " key-value pairs.
        """
        return " | " in text and ": " in text
