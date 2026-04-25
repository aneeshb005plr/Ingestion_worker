# Vector Ingestion Platform — Living Engineering Specification
**Version:** 5.0  
**Status:** 🟢 Active — Updated Mar 22 2026  
**Services:** `ingestion-orchestrator` · `ingestion-worker` · `retrieval-service`  
**Current Tenant:** DocAssist (SharePoint + Local)  
**Stack:** Python 3.13, FastAPI, ARQ, MongoDB Atlas, APScheduler 4.x, LangChain 1.x  
**Current Phase:** C1/C2 Connectors + Retrieval Enhancements Complete ✅  
**Last Completed:** OneDrive + Azure Blob + Reranker + Admin UI planned — Mar 30 2026  
**Status:** 🟢 Active — Updated Mar 30 2026  

> **How to use this document:** This is the single source of truth for the project.
> Every section is updated as we complete each coding phase. When picking up from 
> a new conversation, share this document first — it contains everything needed to 
> continue without repeating context.

---

## Table of Contents

1. [Project Vision & Guiding Principles](#1-project-vision--guiding-principles)
2. [Architecture Overview](#2-architecture-overview)
3. [Folder Structure — Final Target](#3-folder-structure--final-target)
4. [Core Design Decisions](#4-core-design-decisions)
5. [Data Models (MongoDB Collections)](#5-data-models-mongodb-collections)
6. [Tenant & Repository Model](#6-tenant--repository-model)
7. [Ingestion Scheduling System](#7-ingestion-scheduling-system)
8. [Delta Ingestion — Incremental Updates](#8-delta-ingestion--incremental-updates)
9. [Per-Tenant Vector Index Strategy](#9-per-tenant-vector-index-strategy)
10. [Batch & Concurrent Embedding Pipeline](#10-batch--concurrent-embedding-pipeline)
11. [Document Loader System](#11-document-loader-system)
12. [Rich & Customisable Metadata](#12-rich--customisable-metadata)
13. [SharePoint Connector — Production Grade](#13-sharepoint-connector--production-grade)
14. [Job Lifecycle & Observability](#14-job-lifecycle--observability)
15. [Security Architecture](#15-security-architecture)
16. [API Contract](#16-api-contract)
17. [Abstraction Layer — Future-Proofing](#17-abstraction-layer--future-proofing)
18. [Bugs from Original Code Review](#18-bugs-from-original-code-review)
19. [Technology Stack & Dependencies](#19-technology-stack--dependencies)
20. [Coding Plan — Phase by Phase](#20-coding-plan--phase-by-phase)
21. [Progress Tracker](#21-progress-tracker)

---

## 1. Project Vision & Guiding Principles

### What We Are Building
A **multi-tenant document ingestion backbone** that:
- Accepts tenant repositories (SharePoint, local, SQL, MongoDB, future: S3, GDrive)
- Periodically or manually ingests documents from those repositories
- Converts documents to structured Markdown (preserving tables, images, diagrams)
- Chunks, embeds, and stores vectors per tenant with full metadata
- Supports incremental delta updates — only processes changed/new/deleted files
- Is consumed by a separate Retrieval microservice (built later)

### Guiding Principles (Every Line of Code Must Respect These)

**P1 — Abstract Everything That Can Change**  
Vector store, embedding model, document loader, and connector are all behind abstract interfaces. Swapping MongoDB Atlas for Pinecone, or OpenAI for Azure OpenAI, must require zero changes to business logic — only a new implementation class.

**P2 — Tenant Isolation Is Non-Negotiable**  
No query, log, job, credential, or vector can ever cross a tenant boundary. All data paths are keyed by `tenant_id` from entry to storage.

**P3 — Async-First, No Blocking the Event Loop**  
Every I/O operation (DB, HTTP, file) uses `async/await`. CPU-bound work (PDF ML models) runs in `asyncio.to_thread()`. Never use `time.sleep()`, `requests.get()`, or synchronous file reads inside `async def`.

**P4 — Fail Gracefully at Document Level, Not Job Level**  
A single corrupt file must never abort a 500-document job. Per-document errors are caught, logged, and recorded. The job continues.

**P5 — Production Observability from Day One**  
Structured logging (structlog) with `job_id`, `tenant_id`, `repo_id` bound on every log line. No `print()`. No bare `logging.basicConfig`.

**P6 — Repository Pattern for All Data Access**  
No service or business logic directly touches a MongoDB collection. All DB access goes through a Repository class. This makes the system testable and database-agnostic.

**P7 — Configuration Over Code for Tenants**  
Tenant behaviour (concurrency, embedding model, chunk size, metadata schema, schedule) is driven by configuration stored in MongoDB — not by code branches.

---

## 2. Architecture Overview

```
┌────────────────────────────────────────────────────────────────────┐
│                      ingestion-orchestrator                         │
│                           (FastAPI)                                 │
│                                                                     │
│   REST API Layer          Scheduler Layer                           │
│   ─────────────           ───────────────                           │
│   /tenants                APScheduler 4.x                          │
│   /repos                  per-repo cron/interval jobs              │
│   /repos/{id}/ingest  ──► enqueue to ARQ/Redis                    │
│   /jobs/{id}                                                        │
│                                                                     │
│   Repositories: TenantRepo, RepoConfigRepo, JobRepo                │
│   Services:     SchedulerService, JobService                        │
└──────────────────────────────┬─────────────────────────────────────┘
                               │  ARQ Redis Queue
                               │  Payload: {job_id, repo_id, tenant_id}
                               │  (NO credentials in queue — ever)
┌──────────────────────────────▼─────────────────────────────────────┐
│                       ingestion-worker                              │
│                           (ARQ)                                     │
│                                                                     │
│   process_ingestion_task(job_id, repo_id, tenant_id)               │
│                                                                     │
│   1. Load full repo config + credentials from MongoDB              │
│   2. Connector  → fetch delta (new/changed/deleted items only)     │
│   3a. [FILE]  Loader → bytes → Markdown (Docling/MarkItDown)      │
│   3b. [STRUCTURED] Markdown pre-built by connector, skip loader    │
│   4. Chunker    → Markdown-header split → recursive size split     │
│   5. Metadata   → enrich with standard + tenant custom fields      │
│   6. Dedup      → skip unchanged, delete old chunks for changed    │
│   7. Embedder   → batched OpenAI API calls (langchain-openai)      │
│   8. VectorStore→ store in tenant Atlas index (langchain-mongodb)  │
│   9. Registry   → update ingested_documents + job stats            │
│                                                                     │
│   Repositories: RepoConfigRepo, JobRepo, DocumentRepo              │
│   Services:     IngestionService, EmbeddingService, VectorService   │
│   Abstractions: BaseConnector, BaseLoader, BaseVectorStore,        │
│                 BaseEmbeddingProvider                               │
└────────────────────────────────────────────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
         MongoDB Atlas      Redis ARQ       (Future)
         - tenants          - job queue     - S3
         - source_repos     - delta tokens  - blob
         - ingestion_jobs                   - GDrive
         - ingested_docs
         - vector_store.*
```

### Key Separation of Concerns

| Layer | orchestrator | worker |
|-------|-------------|--------|
| Trigger ingestion | ✅ | ❌ |
| Schedule management | ✅ | ❌ |
| Job status reads | ✅ | ❌ |
| Load credentials | ❌ | ✅ |
| Run connectors | ❌ | ✅ |
| Run loaders | ❌ | ✅ |
| Embed & store | ❌ | ✅ |
| Update job status | ❌ | ✅ |

---

## 3. Folder Structure — Final Target

```
vector-ingestion-platform/
│
├── ingestion-orchestrator/
│   ├── main.py
│   ├── requirements.txt
│   ├── .env.example
│   ├── Dockerfile
│   ├── docker-compose.yml       # Redis only (MongoDB is Atlas)
│   │
│   └── app/
│       ├── __init__.py
│       ├── core/
│       │   ├── config.py            # Settings via pydantic-settings
│       │   ├── logging.py           # structlog configuration
│       │   └── exceptions.py        # Custom exception hierarchy
│       │
│       ├── db/
│       │   ├── mongo.py             # Connection lifecycle
│       │   └── indexes.py           # Index creation on startup
│       │
│       ├── queue/
│       │   └── producer.py          # ARQ pool management
│       │
│       ├── repositories/            # ALL MongoDB access lives here
│       │   ├── base.py              # BaseRepository ABC
│       │   ├── tenant_repo.py
│       │   ├── repo_config_repo.py  # source_repositories collection
│       │   └── job_repo.py
│       │
│       ├── services/
│       │   ├── job_service.py       # Create job, enqueue, prevent duplicates
│       │   └── scheduler_service.py # APScheduler lifecycle management
│       │
│       ├── schemas/                 # Pydantic request/response models
│       │   ├── tenant.py
│       │   ├── repo_config.py       # Discriminated union per source type
│       │   ├── job.py
│       │   └── schedule.py
│       │
│       ├── api/
│       │   ├── dependencies.py      # FastAPI Depends() wiring
│       │   ├── health.py
│       │   ├── tenants.py
│       │   ├── repos.py
│       │   ├── schedules.py
│       │   └── jobs.py
│       │
│       └── middleware/
│           ├── auth.py              # API key validation
│           └── rate_limit.py        # Per-tenant rate limiting
│
├── ingestion-worker/
│   ├── app/worker.py                # ARQ WorkerSettings entry point
│   ├── requirements.txt
│   ├── .env.example
│   ├── Dockerfile
│   │
│   └── app/
│       ├── __init__.py
│       ├── core/
│       │   ├── config.py
│       │   ├── logging.py
│       │   └── exceptions.py        # UnsupportedDocumentType, etc.
│       │
│       ├── db/
│       │   ├── mongo.py
│       │   └── indexes.py
│       │
│       ├── repositories/
│       │   ├── base.py
│       │   ├── repo_config_repo.py  # Reads config + credentials
│       │   ├── job_repo.py          # Updates job status & stats
│       │   └── document_repo.py     # ingested_documents delta registry
│       │
│       ├── connectors/              # How we FETCH raw bytes
│       │   ├── base.py              # BaseConnector ABC + RawDocument (Pydantic)
│       │   ├── factory.py
│       │   ├── local.py             # Fully working ✅
│       │   ├── sharepoint.py        # Stub — Phase 6
│       │   ├── sql.py               # Phase 6b (PostgreSQL + MySQL)
│       │   └── mongodb.py           # Phase 6b
│       │
│       ├── loaders/                 # How we PARSE bytes → Markdown
│       │   ├── base.py              # BaseLoader ABC
│       │   ├── factory.py           # get_loader(raw_doc) — routes file + structured
│       │   ├── text_loader.py       # Plain text + Markdown ✅
│       │   ├── pdf_loader.py        # Docling via temp file ✅ (Phase 7: tables/OCR/VLM)
│       │   ├── docx_loader.py       # MarkItDown ✅ (Phase 7: image extraction)
│       │   ├── sql_loader.py        # SQL rows → Markdown table ✅
│       │   ├── mongodb_loader.py    # MongoDB docs → Markdown sections ✅
│       │   └── pptx_loader.py       # Phase 7 placeholder
│       │
│       ├── providers/               # SWAPPABLE external service adapters
│       │   ├── embedding/
│       │   │   ├── base.py          # BaseEmbeddingProvider ABC
│       │   │   ├── openai_provider.py
│       │   │   └── azure_openai_provider.py  # future
│       │   │
│       │   └── vectorstore/
│       │       ├── base.py          # BaseVectorStore ABC
│       │       ├── mongodb_atlas.py
│       │       └── pinecone.py      # future placeholder
│       │
│       ├── pipeline/
│       │   ├── chunker.py              # Two-stage markdown + recursive splitter
│       │   ├── metadata_enricher.py    # Standard + tenant custom metadata
│       │   ├── deduplicator.py         # new/changed/unchanged — NOT deleted (see note)
│       │   └── structured_formatter.py # SQL rows + MongoDB docs → Markdown
│       │
│       └── services/
│           ├── ingestion_service.py # Orchestrates the full pipeline
│           └── job_lifecycle.py     # Status transitions + stats accumulation
│
└── docs/
    ├── SPEC.md                      # This file
    ├── API.md                       # OpenAPI export
    ├── RUNBOOK.md                   # How to run locally + deploy
    └── PROGRESS.md                  # Phase completion notes
```

---

## 4. Core Design Decisions

### 4.1 Multi-Repository Per Tenant, Flexible Index Strategy

A tenant can have **multiple repositories** (e.g. DocAssist might later add a second SharePoint site, or a SQL database). Index assignment is **per repository**, not per tenant.

**Index modes available (set in `source_repositories.vector_config.index_mode`):**

| Mode | Description | Use Case |
|------|-------------|----------|
| `dedicated` | Repo gets its own Atlas index | Large repos, isolated search |
| `shared_tenant` | All tenant repos share one index, filtered by `repo_id` | Small repos, unified search |
| `shared_global` | ⚠️ Not allowed — cross-tenant mixing | Never permitted |

**Default for DocAssist:** `shared_tenant` — one index for all DocAssist repos, filtered by `repo_id` in queries.

### 4.1a Atlas Vector Search Index — Manual Creation Required

> ⚠️ MongoDB Atlas Vector Search indexes are NOT created automatically by pymongo or LangChain.
> They must be created manually via Atlas UI or Atlas Admin API.

**Required index definition** (create in Atlas UI → Atlas Search → Create Vector Search Index):

```json
{
  "fields": [
    {
      "type": "vector",
      "path": "embedding",
      "numDimensions": 1536,
      "similarity": "cosine"
    },
    { "type": "filter", "path": "tenant_id" },
    { "type": "filter", "path": "repo_id" },
    { "type": "filter", "path": "source_id" }
  ]
}
```

Settings:
- Database: `vector_platform`, Collection: `vector_store`
- Index name: must match `source_repositories.vector_config.index_name`
  - Dev/test: `vidx_test`
  - DocAssist production: `vidx_tenant_docassist`
- `numDimensions`: 1536 for `text-embedding-3-small`, 3072 for `text-embedding-3-large`
- `similarity`: cosine — matches `MongoDBAtlasVectorStore(relevance_score_fn="cosine")`
- Filter fields (`tenant_id`, `repo_id`, `source_id`) enable pre-filtering at query time — this is how tenant isolation works at the vector search level

**Setup order — Atlas requires the collection to exist before creating a Vector Search index on it:**

1. Create `vector_store` collection manually in Atlas UI first
   (Atlas UI → Browse Collections → Create Database → `vector_platform` / `vector_store`)
   OR run ingestion once with a small test file — this auto-creates the collection
2. Then create the Vector Search index on the now-existing collection
3. Then run full ingestion

> If you insert documents before the index exists, insertion works fine.
> But similarity search will fail until the index is created and built (~1-2 min).
> Recommended: create empty collection + index BEFORE first ingestion run.

**Phase 8 automation:** Atlas Admin API call to auto-create both the collection and vector index when a new repo is registered via the API. Manual setup is correct for development.

This means:
- When DocAssist searches, they can search across all their repos or filter to one
- Adding a second repo to DocAssist requires zero index changes (same index, new `repo_id`)
- If a repo grows very large, it can be migrated to `dedicated` mode without affecting other repos

### 4.2 Repository Pattern for All DB Access

No service class imports `get_database()` directly. All DB access goes through Repository classes injected via FastAPI's `Depends()`.

```python
# ✅ Correct
class JobService:
    def __init__(self, job_repo: JobRepository, queue: ArqRedis):
        self.job_repo = job_repo
        self.queue = queue

# ❌ Wrong — direct DB access in service
class JobService:
    async def create_job(self):
        db = get_database()
        await db.ingestion_jobs.insert_one(...)
```

This means: to swap MongoDB for Postgres, you only rewrite the Repository implementations. Services and API handlers are untouched.

### 4.3 Provider Abstractions for Vector Store & Embedding

Both the embedding model and vector store sit behind abstract base classes. The concrete implementation is selected at startup based on settings and injected into the worker context.

```python
# app/providers/embedding/base.py
class BaseEmbeddingProvider(ABC):
    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Handles batching internally via LangChain OpenAIEmbeddings."""
        pass

# app/connectors/base.py — RawDocument (Pydantic, not dataclass)
class RawDocument(BaseModel):
    content_type: Literal["file", "structured"]
    content: bytes | None = None          # file bytes
    mime_type: str | None = None          # for file routing in LoaderFactory
    markdown_text: str | None = None      # pre-built for structured sources
    structured_source_type: str | None = None  # "sql" | "mongodb" for structured routing
    source_id: str
    file_name: str
    full_path: str
    source_url: str
    content_hash: str
    last_modified_at_source: datetime | None = None
    extra_metadata: dict = {}

# app/providers/vectorstore/base.py  
class BaseVectorStore(ABC):
    @abstractmethod
    async def add_documents(
        self, chunks: list[VectorChunk], tenant_id: str, repo_id: str
    ) -> None: pass

    @abstractmethod
    async def delete_by_source(
        self, source_id: str, tenant_id: str, repo_id: str
    ) -> int: pass  # returns count deleted
```

Swapping from MongoDB Atlas to Pinecone = add `PineconeVectorStore(BaseVectorStore)`, change one config value. Zero other changes.

### 4.4 Credentials Never Touch the Queue

The ARQ job payload contains **only safe references**:
```python
# What goes into Redis queue — only IDs
{"job_id": "...", "repo_id": "...", "tenant_id": "..."}

# What the worker does at startup — resolve credentials from DB
repo_config = await repo_config_repo.get_with_credentials(repo_id)
# credentials are in memory only, for this job only
```

### 4.5 Structured Logging with Context Binding

Every module gets a structlog logger. The worker task binds job context at the start and uses it throughout:

```python
import structlog

# In worker task — bind once, use everywhere
log = structlog.get_logger().bind(
    job_id=job_id, tenant_id=tenant_id, repo_id=repo_id
)
log.info("ingestion.started")
log.info("document.processing", file="Q1_Report.pdf", mime="application/pdf")
log.warning("document.skipped", reason="unchanged", file="old.pdf")
log.error("document.failed", file="corrupt.pdf", error=str(e))
log.info("ingestion.completed", new=12, updated=3, deleted=1, skipped=126)
```

---

## 5. Data Models (MongoDB Collections)

### 5.1 `tenants`
```json
{
  "_id": "tenant_docassist",
  "name": "DocAssist",
  "slug": "docassist",
  "is_active": true,
  "created_at": "2026-01-15T10:00:00Z",
  "ingestion_defaults": {
    "embedding_provider": "openai",
    "embedding_model": "text-embedding-3-small",
    "chunk_size": 1024,
    "chunk_overlap": 150,
    "document_concurrency": 10,
    "embedding_batch_size": 512,
    "embedding_concurrency": 5
  },
  "metadata_schema": {
    "custom_fields": [
      {
        "field_name": "department",
        "source": "path_segment",
        "path_segment_index": 1,
        "default": "unknown"
      }
    ]
  }
}
```

### 5.2 `source_repositories`
```json
{
  "_id": "repo_sp_docassist_main",
  "tenant_id": "tenant_docassist",
  "name": "DocAssist Main SharePoint",
  "source_type": "sharepoint",
  "is_active": true,
  "created_at": "2026-01-15T10:00:00Z",

  "credentials_ref": "cred_sp_docassist_main",

  "connector_config": {
    "site_id": "docassist.sharepoint.com,abc,xyz",
    "drive_id": "b!...",
    "root_folder": "/Shared Documents",
    "recursive": true,
    "file_extensions_include": [".pdf", ".docx", ".pptx", ".xlsx", ".txt"],
    "file_extensions_exclude": [".tmp"],
    "max_file_size_mb": 50,
    "skip_hidden_files": true
  },

  "vector_config": {
    "index_mode": "shared_tenant",
    "index_name": "vidx_tenant_docassist",
    "vector_dimensions": 1536,
    "similarity": "cosine"
  },

  "schedule": {
    "mode": "interval",
    "interval_minutes": 60,
    "cron_expression": null,
    "timezone": "Europe/London",
    "enabled": true,
    "next_run_at": "2026-02-23T11:00:00Z",
    "last_triggered_at": "2026-02-23T10:00:00Z"
  },

  "ingestion_overrides": {
    "chunk_size": null,
    "chunk_overlap": null,
    "document_concurrency": null
  },

  "state": {
    "delta_token": null,
    "last_successful_run_at": null,
    "total_documents_indexed": 0
  }
}
```

### 5.3 `credentials` (encrypted)
```json
{
  "_id": "cred_sp_docassist_main",
  "tenant_id": "tenant_docassist",
  "repo_id": "repo_sp_docassist_main",
  "type": "sharepoint_app",
  "data": {
    "azure_tenant_id": "<encrypted>",
    "client_id": "<encrypted>",
    "client_secret": "<encrypted>"
  },
  "created_at": "2026-01-15T10:00:00Z"
}
```

### 5.4 `ingestion_jobs`
```json
{
  "_id": "job_xyz789",
  "tenant_id": "tenant_docassist",
  "repo_id": "repo_sp_docassist_main",
  "trigger": "scheduled",
  "status": "completed",
  "created_at": "2026-02-23T10:00:00Z",
  "started_at": "2026-02-23T10:00:05Z",
  "completed_at": "2026-02-23T10:15:30Z",
  "duration_seconds": 925,
  "arq_job_id": "arq:job:abc123",
  "stats": {
    "documents_scanned": 142,
    "documents_new": 12,
    "documents_updated": 3,
    "documents_deleted": 1,
    "documents_skipped": 126,
    "documents_failed": 0,
    "chunks_created": 847,
    "chunks_deleted": 61
  },
  "errors": [
    {"file": "corrupt.pdf", "error": "PDF parsing failed: ..."}
  ]
}
```

### 5.5 `ingested_documents`
```json
{
  "_id": "doc_abc456",
  "tenant_id": "tenant_docassist",
  "repo_id": "repo_sp_docassist_main",
  "source_id": "01SHAREPOINT_ITEM_ID",
  "file_name": "Q1_Report.pdf",
  "full_path": "/Shared Documents/Finance/Q1_Report.pdf",
  "source_url": "https://docassist.sharepoint.com/sites/.../Q1_Report.pdf",
  "mime_type": "application/pdf",
  "content_hash": "sha256:abc123...",
  "last_modified_at_source": "2026-02-20T14:30:00Z",
  "last_ingested_at": "2026-02-23T10:05:00Z",
  "chunk_count": 47,
  "status": "active",
  "job_id": "job_xyz789"
}
```

### 5.6 MongoDB Indexes (Created on Startup)
```python
# tenants
await db.tenants.create_index("slug", unique=True)

# source_repositories
await db.source_repositories.create_index("tenant_id")
await db.source_repositories.create_index([("tenant_id", 1), ("is_active", 1)])

# ingestion_jobs
await db.ingestion_jobs.create_index([("repo_id", 1), ("status", 1)])
await db.ingestion_jobs.create_index([("tenant_id", 1), ("created_at", -1)])

# ingested_documents
await db.ingested_documents.create_index(
    [("tenant_id", 1), ("repo_id", 1), ("source_id", 1)], unique=True
)
await db.ingested_documents.create_index([("repo_id", 1), ("status", 1)])
await db.ingested_documents.create_index("content_hash")

# credentials
await db.credentials.create_index([("tenant_id", 1), ("repo_id", 1)])
```

---

## 6. Tenant & Repository Model

### 6.1 Multi-Repo per Tenant

Each tenant can have N repositories. Each repo independently has:
- Its own connector type (sharepoint, local, sql, mongodb)
- Its own connector config (site, folder, query)
- Its own schedule (or manual-only)
- Its own vector index assignment (`index_mode`)
- Its own ingestion overrides (chunk size, concurrency — overrides tenant defaults)

### 6.2 Effective Config Resolution (Tenant Defaults + Repo Overrides)

The worker resolves the effective config at job start:
```python
def resolve_ingestion_config(tenant: Tenant, repo: SourceRepository) -> IngestionConfig:
    defaults = tenant.ingestion_defaults
    overrides = repo.ingestion_overrides
    return IngestionConfig(
        chunk_size=overrides.chunk_size or defaults.chunk_size,
        chunk_overlap=overrides.chunk_overlap or defaults.chunk_overlap,
        document_concurrency=overrides.document_concurrency or defaults.document_concurrency,
        embedding_model=defaults.embedding_model,  # model is always tenant-level
    )
```

---

## 7. Ingestion Scheduling System

### 7.1 APScheduler 4.x in the Orchestrator

APScheduler runs inside the FastAPI process as an `AsyncIOScheduler`. On startup it loads all active repos and registers jobs.

```python
# In lifespan
scheduler = AsyncIOScheduler()

async def trigger_repo_ingestion(repo_id: str, tenant_id: str):
    """Called by APScheduler — enqueues to ARQ, doesn't run inline."""
    await job_service.trigger_ingestion(repo_id, tenant_id, trigger="scheduled")

# Load all active scheduled repos on startup
repos = await repo_config_repo.get_all_scheduled()
for repo in repos:
    scheduler_service.register_repo(repo)

scheduler.start()
```

### 7.2 Schedule Modes

```python
def register_repo(self, repo: SourceRepository) -> None:
    if repo.schedule.mode == "interval":
        self.scheduler.add_job(
            trigger_repo_ingestion,
            trigger=IntervalTrigger(minutes=repo.schedule.interval_minutes,
                                    timezone=repo.schedule.timezone),
            id=f"repo_{repo.id}",
            kwargs={"repo_id": repo.id, "tenant_id": repo.tenant_id},
            replace_existing=True,
            misfire_grace_time=300,     # 5 min grace for missed fires
            coalesce=True,              # if missed multiple, only run once
        )
    elif repo.schedule.mode == "cron":
        self.scheduler.add_job(
            trigger_repo_ingestion,
            trigger=CronTrigger.from_crontab(repo.schedule.cron_expression,
                                             timezone=repo.schedule.timezone),
            id=f"repo_{repo.id}",
            kwargs={"repo_id": repo.id, "tenant_id": repo.tenant_id},
            replace_existing=True,
        )
```

### 7.3 Concurrency Guard (One Job Per Repo at a Time)

```python
async def trigger_ingestion(self, repo_id: str, tenant_id: str, trigger: str) -> str:
    # Check for in-flight job
    existing = await self.job_repo.get_active_job_for_repo(repo_id)
    if existing:
        raise JobAlreadyRunningError(
            f"Job {existing.id} is already {existing.status} for repo {repo_id}"
        )
    # ... proceed to create and enqueue
```

Manual trigger (POST /repos/{id}/ingest) → `409 Conflict` if already running.  
Scheduled trigger → logs warning and skips.

---

## 8. Delta Ingestion — Incremental Updates

### 8.1 SharePoint Delta API (Primary Strategy)

Microsoft Graph exposes `/drive/root/delta` which returns a `deltaLink` (sync token). On the first run, we do a full scan and save the `deltaLink` in `source_repositories.state.delta_token`. On subsequent runs, we call the deltaLink endpoint and only receive files that changed.

```
Run 1 (no delta_token): GET /drive/root/delta → all files + deltaLink saved
Run 2 (has delta_token): GET <deltaLink>       → only changed/deleted files
```

This means for a 10,000 file SharePoint site, runs 2..N scan only the N changed files — not 10,000 files every hour.

### 8.2 Content Hash Fallback (Local, SQL, MongoDB Connectors)

For connectors without a native delta API:
1. Compute `sha256` of file content
2. Look up `(tenant_id, repo_id, source_id)` in `ingested_documents`
3. If hash matches stored hash → skip (unchanged)
4. If hash differs → process as updated document

### 8.3 Deleted File Handling

During each run, the connector yields `DeltaItem` objects that can be type `new`, `modified`, or `deleted`.

For SharePoint delta API: deleted items are explicitly returned with `@removed` property.  
For content hash strategy: after scanning, compare scanned `source_id` set against registry — any registry document not in the scan set is marked deleted.

On deletion:
1. Delete all chunks from vector store by `(source_id, tenant_id, repo_id)`
2. Update `ingested_documents.status = "deleted"`
3. Record in job stats

### 8.4 Re-embedding Updated Documents

When a file has changed:
1. Delete old chunks from vector store (by source_id filter)
2. Process the new version through the full pipeline
3. Update `ingested_documents` with new hash, chunk count, etc.

---

## 9. Per-Tenant Vector Index Strategy

### 9.1 Index Assignment Rules

| `index_mode` | Index Name | Used When |
|-------------|------------|-----------|
| `shared_tenant` | `vidx_tenant_{tenant_id}` | Default — all repos of a tenant share one index |
| `dedicated` | `vidx_repo_{repo_id}` | Explicitly set per repo for large/isolated repos |

The retrieval service is given the index name from the `source_repositories` record — it never hard-codes index names.

### 9.2 Index Provisioning on Repo Creation

When a repo is created with `index_mode = "dedicated"`, the orchestrator automatically creates the Atlas Search index via the Atlas Admin API. For `shared_tenant`, the index is created when the first repo with that tenant is registered, and reused for subsequent repos.

### 9.3 Atlas Index Definition Template

```python
def build_index_definition(index_name: str, dimensions: int) -> dict:
    return {
        "name": index_name,
        "type": "vectorSearch",
        "definition": {
            "fields": [
                {"type": "vector", "path": "embedding",
                 "numDimensions": dimensions, "similarity": "cosine"},
                {"type": "filter", "path": "tenant_id"},
                {"type": "filter", "path": "repo_id"},
                {"type": "filter", "path": "source_id"},
                {"type": "filter", "path": "element_type"},  # text/table/image_desc
            ]
        }
    }
```

---

## 10. Batch & Concurrent Embedding Pipeline

### 10.1 Two Levels of Concurrency

**Level 1 — Document Concurrency (within a job):**
- Multiple documents processed concurrently inside `process_ingestion_task`
- Controlled by `asyncio.Semaphore(repo_config.document_concurrency)`
- Default: 10 concurrent documents

**Level 2 — Embedding Batch Concurrency (within a document):**
- Chunks split into batches of `embedding_batch_size` (default: 512)
- Batches sent to OpenAI concurrently
- Controlled by `asyncio.Semaphore(repo_config.embedding_concurrency)`
- Default: 5 concurrent OpenAI API calls

### 10.2 Retry with Exponential Backoff

All OpenAI embedding calls are wrapped with tenacity:
```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(5),
)
async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
    response = await self.client.embeddings.create(
        model=self.model, input=texts
    )
    return [item.embedding for item in response.data]
```

### 10.3 EmbeddingProvider ABC

```python
class BaseEmbeddingProvider(ABC):
    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts. Handles batching internally."""
        pass

    @property
    @abstractmethod
    def dimensions(self) -> int:
        pass
```

The orchestration in `IngestionService` calls `provider.embed_documents(all_chunk_texts)` — it doesn't know or care whether the provider is OpenAI, Azure OpenAI, or a local model.

---

## 11. Document Loader System

### 11.1 BaseLoader ABC

```python
class BaseLoader(ABC):
    @abstractmethod
    async def load(self, raw_doc: RawDocument) -> ParsedDocument:
        """
        Transform raw bytes into structured Markdown.
        Returns ParsedDocument with markdown_text + extracted_metadata.
        """
        pass

    @abstractmethod
    def supported_mime_types(self) -> list[str]:
        pass
```

`ParsedDocument` carries back loader-extracted metadata (page count, author, etc.) that gets merged into the chunk metadata.

### 11.2 PDF Loader

- **Library:** `pymupdf4llm` (pure C, zero model downloads, corporate-safe — no HuggingFace)
- **Previous library:** `docling` — removed because it requires ~1.5GB HuggingFace model downloads, blocked in corporate/PwC environments
- **Two-stage pipeline:**

  **Stage 1 — always runs (free, offline):**
  ```
  pymupdf opens PDF → inspect each page (char count + image count)
  → pymupdf4llm.to_markdown() → structured Markdown
  ```

  **Stage 2 — only fires when complexity detected:**
  | Trigger | LLM action |
  |---|---|
  | Scanned PDF (avg <50 chars/page) | Vision OCR — each page rendered as PNG → GPT-4o → Markdown |
  | Embedded images | Vision description appended to Markdown |
  | Scanned > 30 pages | Capped at 30 pages, note appended |

- **LLM used:** `langchain_openai.ChatOpenAI(model="gpt-4o")` — uses existing approved endpoint
- **If LLM not configured:** Library-only mode, scanned PDFs log a warning and return sparse text
- **Key config:** `_SCANNED_TEXT_THRESHOLD = 50` chars/page, `_MAX_PAGES_FOR_VISION = 30`

### 11.3 DOCX Loader

- **Library:** `MarkItDown` (Microsoft) — handles headings, lists, tables, XLSX, PPTX
- **Two-stage pipeline:**

  **Stage 1 — always runs (free, offline):**
  ```
  python-docx inspects XML (~10ms) → detect images + nested tables
  → MarkItDown.convert_stream() → Markdown
  ```

  **Stage 2 — only fires when complexity detected:**
  | Trigger | Detection method | LLM action |
  |---|---|---|
  | Images (`inline_shapes > 0`) | `python-docx inline_shapes` | Vision description per image |
  | Nested tables (`cell.tables`) | `python-docx cell.tables` check | LLM cleanup of garbled output |

- **LLM used:** Same `ChatOpenAI(model="gpt-4o")` instance as PDF loader
- **Known gap:** Scanned DOCX (image-only pages) not yet handled — Phase 7
- **MarkItDown known gaps:** Nested tables silently lost without LLM stage (our fix handles this)

### 11.3 Word Loader (.docx)

- MarkItDown for text + heading structure
- python-docx to extract embedded images separately
- Each image → GPT-4o Vision → `[IMAGE: {alt_text}]` placeholder inserted
- Document properties (author, created, modified) extracted and added to metadata
- Tables with merged cells → custom Markdown conversion via python-docx table parser

### 11.4 PowerPoint Loader (.pptx)

- Each slide processed independently
- `slide_number` stored in chunk metadata
- Slide titles become `##` headings in Markdown
- Charts and images described via VLM

### 11.5 Loader Factory with Strict Error Handling

```python
def get_loader(self, mime_type: str) -> BaseLoader:
    loader = self._registry.get(mime_type)
    if loader is None:
        raise UnsupportedDocumentTypeError(
            f"No loader registered for MIME type: {mime_type}"
        )
    return loader
```

`UnsupportedDocumentTypeError` is caught at the document level in the worker — the document is marked `skipped` with reason `unsupported_mime_type`. The job continues.

---

## 12. Rich & Customisable Metadata

### 12.1 Standard Metadata (All Chunks, Always Present)

```python
@dataclass
class ChunkMetadata:
    # Tenant + job context
    tenant_id: str
    repo_id: str
    job_id: str
    document_id: str

    # Chunk position
    chunk_index: int
    chunk_total: int

    # File identity
    file_name: str
    file_extension: str
    mime_type: str
    content_hash: str

    # Full location — always populated
    full_path: str         # /Shared Documents/Finance/Q1.pdf
    source_url: str        # https://sharepoint.com/.../Q1.pdf
    source_type: str       # sharepoint, local, sql, mongodb

    # Temporal
    created_at_source: datetime | None
    last_modified_at_source: datetime | None
    ingested_at: datetime

    # Document structure
    page_start: int | None
    page_end: int | None
    slide_number: int | None
    section_headers: list[str]  # heading breadcrumb at this chunk

    # Content classification
    element_type: str           # "text" | "table" | "image_description"
    contains_table: bool
    contains_image_description: bool
    language: str | None

    # Source-specific extras (populated per connector)
    extra: dict[str, Any] = field(default_factory=dict)
```

### 12.2 SharePoint-Specific Extras

Added to `extra` dict:
```python
{
    "sharepoint_item_id": "01ABC...",
    "sharepoint_drive_id": "b!...",
    "sharepoint_author": "John Smith",
    "sharepoint_last_modified_by": "Jane Doe",
    "sharepoint_version": "2.0"
}
```

### 12.3 Tenant Custom Metadata Fields

The `MetadataEnricher` reads `tenant.metadata_schema.custom_fields` and populates them per document:

```python
# Example: extract department from path /Shared Documents/{dept}/...
{
    "field_name": "department",
    "source": "path_segment",
    "path_segment_index": 1,
    "default": "unknown"
}
# → chunk.metadata["department"] = "Finance"
```

Supported `source` values:
- `path_segment` — extract nth segment from full_path
- `path_pattern` — regex match against full_path or file_name
- `file_property` — SharePoint column or document property
- `static` — constant value for all docs in this repo

---

## 13. SharePoint Connector — Production Grade

### 13.1 Required Capabilities

- ✅ OAuth2 App-Only authentication (ClientSecretCredential)
- ✅ Graph SDK `msgraph-sdk-python`
- ✅ Full delta API (`/drive/root/delta`) with token persistence
- ✅ Recursive folder traversal with full path tracking
- ✅ Pagination (`@odata.nextLink`) — handles 10,000+ files
- ✅ File type filtering (include/exclude extensions)
- ✅ Hidden file filtering (skip files starting with `~$`)
- ✅ File size limit (skip files above `max_file_size_mb`)
- ✅ Per-file error isolation (one bad file doesn't stop the job)
- ✅ Streaming download for large files
- ✅ `full_path` and `source_url` populated for every document

### 13.2 Delta Loop

```python
async def fetch_documents(self) -> AsyncGenerator[DeltaItem, None]:
    delta_url = self.delta_token or f"drives/{self.drive_id}/root/delta"
    
    while delta_url:
        page = await self._get_with_retry(delta_url)
        for item in page["value"]:
            if "@removed" in item:
                yield DeltaItem(type="deleted", source_id=item["id"], ...)
            elif item.get("file") and self._should_process(item):
                content = await self._download(item)
                yield DeltaItem(type="upsert", source_id=item["id"],
                                raw_doc=RawDocument(...), ...)
        delta_url = page.get("@odata.nextLink")
    
    self.final_delta_link = page.get("@odata.deltaLink")
    # Saved to DB after successful run
```

---


### Connector Summary Table

| Connector | File | Status | supports_native_deletes | delta_token |
|---|---|---|---|---|
| Local filesystem | `local_connector.py` | ✅ Complete | False | No |
| SharePoint Online | `sharepoint_connector.py` | 🔲 Phase 6 stub | True | Yes (Phase 6) |
| MongoDB collection | `mongodb_connector.py` | ✅ Complete | False | No |
| SQL table | `sql_connector.py` | ✅ Complete | False | No |

**Delete detection strategy by connector type:**
- `supports_native_deletes=True` (SharePoint): connector yields `DeltaItem(type="deleted")` inline — worker handles in Step 4
- `supports_native_deletes=False` (Local, SQL, MongoDB): worker calls `deduplicator.check_for_deletions()` in Step 5 — compares `seen_source_ids` vs registry

## 14. Job Lifecycle & Observability

### 14.1 Status Transitions

```
PENDING → PROCESSING → COMPLETED
                     → FAILED       (unrecoverable error)
                     → PARTIAL      (some docs failed, job continued)
```

Stats are accumulated in-memory during the job and written to MongoDB at completion (one write, not one per document).

### 14.2 Worker Status Updates

```python
# On task start
await job_lifecycle.mark_processing(job_id)

# On each document result — accumulate in memory
stats_accumulator.record_result(doc_result)  # new/updated/skipped/failed/deleted

# On task completion (one DB write)
await job_lifecycle.mark_completed(job_id, stats_accumulator.get_stats())

# On task failure
await job_lifecycle.mark_failed(job_id, error=str(e))
```

### 14.3 ARQ Retry Configuration

```python
class WorkerSettings:
    functions = [process_ingestion_task]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(str(settings.REDIS_URI))
    queue_name = settings.QUEUE_NAME
    max_jobs = 5          # concurrent ARQ tasks per worker process
    job_timeout = 3600    # 1 hour hard limit
    keep_result = 3600    # keep ARQ result for 1 hour
```

Note: ARQ retries are not used for the whole job — instead, per-document retry via tenacity. A job failing permanently means something systemic.

### 14.4 Health Endpoints

```
GET /health/live   → 200 if process is running (no DB check)
GET /health/ready  → 200 if MongoDB + Redis both healthy, else 503
```

---

## 15. Security Architecture

### 15.1 API Authentication (Phase 8)

- API key per tenant (`X-API-Key` header)
- Keys stored as `bcrypt(key)` hash in `api_keys` collection
- Keys are validated in FastAPI middleware, not per-route
- Tenant context injected into all requests via `Depends(get_current_tenant)`

### 15.2 Credentials Never in Queue

Redis job payload = `{job_id, repo_id, tenant_id}` only.  
Worker reads credentials from MongoDB using `repo_id` at task start.  
Credentials live in worker memory only for the duration of the task.

### 15.3 Credential Storage (Phase 1 Simplified, Phase 8 Full)

**Phase 1 (now):** Credentials stored in `credentials` collection in plaintext for development. Access restricted by `tenant_id` repo ownership check.

**Phase 8 (hardening):** Migrate to encrypted field storage using PyMongo CSFLE, or replace with Azure Key Vault / AWS Secrets Manager reference.

### 15.4 Structlog Credential Scrubbing

A structlog processor scrubs known sensitive key names before any log output:
```python
SENSITIVE_KEYS = {"client_secret", "password", "api_key", "token", "authorization"}

def scrub_sensitive(logger, method, event_dict):
    for key in SENSITIVE_KEYS:
        if key in event_dict:
            event_dict[key] = "***REDACTED***"
    return event_dict
```

---

## 16. API Contract

### Orchestrator — Full Endpoints

```
# Tenants
POST   /api/v1/tenants
GET    /api/v1/tenants/{tenant_id}
PATCH  /api/v1/tenants/{tenant_id}

# Repositories
POST   /api/v1/tenants/{tenant_id}/repos
GET    /api/v1/tenants/{tenant_id}/repos
GET    /api/v1/repos/{repo_id}
PATCH  /api/v1/repos/{repo_id}
DELETE /api/v1/repos/{repo_id}

# Schedule Management
GET    /api/v1/repos/{repo_id}/schedule
PUT    /api/v1/repos/{repo_id}/schedule
POST   /api/v1/repos/{repo_id}/schedule/pause
POST   /api/v1/repos/{repo_id}/schedule/resume

# Ingestion Trigger
POST   /api/v1/repos/{repo_id}/ingest      → 202 {job_id} or 409 if running

# Jobs
GET    /api/v1/jobs/{job_id}
GET    /api/v1/repos/{repo_id}/jobs?page=1&limit=20
GET    /api/v1/tenants/{tenant_id}/jobs?status=running

# Document Registry (for retrieval service)
GET    /api/v1/repos/{repo_id}/documents

# Health
GET    /health/live
GET    /health/ready
```

### Pydantic Schema Design: Discriminated Union for Connector Config

```python
class SharePointConnectorConfig(BaseModel):
    source_type: Literal["sharepoint"]
    site_id: str
    drive_id: str
    root_folder: str = "/Shared Documents"
    recursive: bool = True
    file_extensions_include: list[str] = [".pdf", ".docx", ".pptx"]
    file_extensions_exclude: list[str] = [".tmp"]
    max_file_size_mb: int = 50

class LocalConnectorConfig(BaseModel):
    source_type: Literal["local"]
    directory_path: str
    recursive: bool = True
    file_extensions_include: list[str] = [".pdf", ".docx", ".txt"]

ConnectorConfig = Annotated[
    SharePointConnectorConfig | LocalConnectorConfig,
    Field(discriminator="source_type")
]

class CreateRepoRequest(BaseModel):
    name: str
    connector_config: ConnectorConfig
    credentials: dict[str, str]  # validated by connector type
    schedule: ScheduleConfig
    vector_config: VectorConfig | None = None  # uses tenant defaults if None
```

---

## 17. Abstraction Layer — Future-Proofing

### 17.1 What Can Change and How to Handle It

| Thing That Can Change | How We Handle It |
|----------------------|-----------------|
| Vector store (Atlas → Pinecone) | `BaseVectorStore` ABC — add new implementation, change config |
| Embedding model (OpenAI → Azure OpenAI) | `BaseEmbeddingProvider` ABC — add new implementation |
| Embedding dimensions (1536 → 3072) | Stored in `vector_config.vector_dimensions` per repo |
| New connector (S3, GDrive) | `BaseConnector` ABC — register in factory, zero other changes |
| New loader (HTML, email) | `BaseLoader` ABC — register in factory, zero other changes |
| Database (MongoDB → something else) | Repository pattern — rewrite Repo classes only, services untouched |
| Chunking strategy | `Chunker` is a standalone class, injectable |

### 17.2 Provider Selection at Runtime

```python
# In worker startup — provider selected based on tenant config
def get_embedding_provider(config: IngestionConfig) -> BaseEmbeddingProvider:
    providers = {
        "openai": OpenAIEmbeddingProvider,
        "azure_openai": AzureOpenAIEmbeddingProvider,
    }
    cls = providers.get(config.embedding_provider)
    if cls is None:
        raise ValueError(f"Unknown embedding provider: {config.embedding_provider}")
    return cls(config)
```

---

## 17b. Bugs Found During Phase 1 Testing

These bugs were discovered during the first real test run (Feb 25 2026) and fixed immediately.

| # | File | Bug | Fix |
|---|------|-----|-----|
| T1 | `db/mongo.py` | `datetime` from MongoDB returned as naive (no timezone). Subtracting from UTC-aware `now` raised `TypeError: can't subtract offset-naive and offset-aware datetimes` | Added `tz_aware=True` to `AsyncMongoClient` — all datetimes now return with UTC tzinfo |
| T2 | `providers/vectorstore/mongodb_atlas.py` | `LangChain MongoDBAtlasVectorSearch` uses sync `bulk_write` internally, incompatible with `AsyncMongoClient`. `RuntimeWarning: coroutine never awaited` | Created separate sync `MongoClient` for LangChain. Switched to direct `insert_many()` with pre-computed embeddings — LangChain does not support pre-computed embeddings in `add_documents()` |
| T3 | `loaders/pdf_loader.py` | Docling's `DocumentConverter.convert()` does not accept `BytesIO` — requires a file path (`str` or `Path`) | Write bytes to `NamedTemporaryFile`, pass path to Docling, clean up in `finally` block |
| T4 | `loaders/factory.py` | `get_loader(mime_type: str)` signature only handled file types — structured sources (SQL/MongoDB) have no mime_type | Changed to `get_loader(raw_doc: RawDocument)` — factory inspects `content_type` and routes to correct registry. `ingestion_service.py` updated to pass `raw_doc` |

---

## 18. Bugs from Original Code Review

All 5 critical bugs must be fixed in Phase 1 before any new features.

| ID | File | Bug | Fix |
|----|------|-----|-----|
| B1 | `worker/db/mongo.py` | `connect_to_mongo()` called with URI arg but takes none | Remove arg from call site |
| B2 | `LocalConnector` | `self.base_path` → should be `self.path` | Rename |
| B3 | `DoclingPDFLoader` | `asyncio.threads()` → `asyncio.to_thread()` | Replace call |
| B4 | `SharePointConnector` | `__init` typo → `__init__` | Fix typo |
| B5 | `jobs.py` API | `config` received but not passed to service | Pass it |

Medium issues also fixed in Phase 1:
- `PROJECt_NAME` → `PROJECT_NAME`
- `REDIS_HOST`/`REDIS_PORT` not in Settings — use `from_dsn()`
- `get_database()` in document loop — resolve once
- `afrom_documents` per doc → instantiate vectorstore once, use `aadd_documents`
- `print()` in LocalConnector → structlog

---

## 19. Technology Stack & Dependencies

### ingestion-orchestrator (`requirements.txt`)

> Uses `requirements.txt`, not `pyproject.toml` — confirmed during Phase 1.

```
fastapi>=0.115.0
uvicorn[standard]>=0.32.0       # uvloop + httptools for speed
pydantic>=2.9.0
pydantic-settings>=2.6.0
pymongo>=4.10.0                 # AsyncMongoClient (native async)
arq>=0.26.0                     # Queue producer
apscheduler>=4.0.0a5            # Scheduling
structlog>=24.4.0               # Structured logging
tenacity>=9.0.0                 # Retry logic
scalar-fastapi>=1.0.0           # Scalar UI (+ Swagger at /docs)
```

> Note: `motor` removed — `pymongo>=4.10` includes `AsyncMongoClient` natively.
> Note: `slowapi` deferred to Phase 8 (Security Hardening).

### ingestion-worker (`requirements.txt`)

```
arq>=0.26.0
pydantic>=2.9.0
pydantic-settings>=2.6.0
pymongo>=4.10.0
structlog>=24.4.0
tenacity>=9.0.0
# LangChain — used where it genuinely solves hard problems
langchain-core>=0.3.0               # Document, Embeddings base classes
langchain-text-splitters>=0.3.0     # MarkdownHeaderTextSplitter + RecursiveCharacterTextSplitter
langchain-openai>=0.2.0             # OpenAIEmbeddings (replaces raw openai lib)
langchain-mongodb>=0.3.0            # MongoDBAtlasVectorSearch
# Connectors
aiofiles>=24.1.0
msgraph-sdk>=1.9.0
azure-identity>=1.19.0
# Loaders
docling>=2.10.0                     # PDF: structure-aware, tables, OCR
markitdown[all]>=0.1.0              # DOCX/PPTX/XLSX → Markdown
python-docx>=1.1.0                  # DOCX image extraction (Phase 7)
orjson>=3.10.0
```

> Note: `openai` lib removed — `langchain-openai` brings it in as a dependency.
> Note: `python-pptx` deferred to Phase 7.
> Note: `asyncpg>=0.30.0` and `aiomysql>=0.2.0` added in Phase 6b for SQL connector.
> Note: `pymongo` already present — MongoDB connector needs no new dependency.

### LangChain Usage Policy (decided Phase 1)

| What | LangChain used? | Why |
|------|----------------|-----|
| Text splitting | ✅ `langchain-text-splitters` | MarkdownHeaderTextSplitter is excellent, no alternative |
| Vector store | ✅ `langchain-mongodb` | Handles Atlas query format, gives retrieval service `similarity_search()` for free |
| Embeddings | ✅ `langchain-openai` internally | Stable, well-maintained, handles batching — wrapped by our `BaseEmbeddingProvider` ABC |
| Document loading | ❌ | Docling + MarkItDown produce better quality output (tables, structure) |
| Chains / agents | ❌ | Not needed — we have our own pipeline |
| LLM calls | ❌ | Direct OpenAI SDK via langchain-openai dependency |

---

## 20. Coding Plan — Phase by Phase

We code one phase at a time. Each phase produces working, committed code. No phase is considered done until it runs end-to-end and the progress tracker below is updated.

---

### 📦 Phase 1 — Project Foundation & Bug Fixes
**Goal:** Clean, runnable skeleton of both services with all bugs fixed. No new features.

**Deliverables:**
1. `pyproject.toml` for both services (replace raw `requirements.txt`)
2. `app/core/config.py` — pydantic-settings, fixed `PROJECT_NAME`, `REDIS_URI` properly parsed
3. `app/core/logging.py` — structlog configured, JSON in prod, colored in dev
4. `app/core/exceptions.py` — custom exception hierarchy
5. `app/db/mongo.py` (both) — async connection, index creation on startup, fixed signature
6. `app/queue/producer.py` — fixed `RedisSettings.from_dsn`
7. `LocalConnector` — fix `self.path`, fix `print()` → structlog
8. `DoclingPDFLoader` — fix `asyncio.to_thread()`
9. `SharePointConnector` — fix `__init__`, placeholder implementation
10. `jobs.py` API — fix `config` being dropped
11. `app/api/health.py` — real liveness + readiness endpoints
12. Both services run locally with `docker-compose up`

**Acceptance:** Both services start. Health endpoints return 200. POST /api/v1/repos/{id}/ingest returns 202. Worker receives the job and logs it.

---

### 📦 Phase 2 — Repository Pattern & Data Layer
**Goal:** All MongoDB access behind Repository classes. Data models established.

**Deliverables:**
1. `app/repositories/base.py` — BaseRepository ABC
2. Orchestrator repos: `TenantRepository`, `RepoConfigRepository`, `JobRepository`
3. Worker repos: `RepoConfigRepository`, `JobRepository`, `DocumentRepository`
4. All existing services refactored to use repos (no direct `db.collection.find()` in services)
5. `app/db/indexes.py` — all indexes created on startup
6. Pydantic schemas with discriminated union for `ConnectorConfig`

**Acceptance:** Can create tenant + repo via API. Repo config is stored and readable. Job created and persisted.

---

### 📦 Phase 3 — Tenant & Repository Management API
**Goal:** Full CRUD for tenants and repos. Manual trigger works end-to-end.

**Deliverables:**
1. `POST /api/v1/tenants` — create tenant with defaults
2. `POST /api/v1/tenants/{id}/repos` — register repo with SharePoint or Local config
3. `GET /api/v1/repos/{id}` — read repo details
4. `POST /api/v1/repos/{id}/ingest` — manual trigger, 409 guard
5. `GET /api/v1/jobs/{id}` — job status
6. `GET /api/v1/repos/{id}/jobs` — job history
7. Job lifecycle in worker: PENDING → PROCESSING → COMPLETED/FAILED

**Acceptance:** Register DocAssist tenant + SharePoint repo via API. Trigger manual run. Worker picks it up, runs (even if connector is a stub), marks job completed. Check job status via GET endpoint.

---

### 📦 Phase 4 — Delta Ingestion & Document Registry
**Goal:** Only new/changed files are processed. Deletes tracked.

**Deliverables:**

> Note: Items 1-5 below were scaffolded in Phase 1. Phase 4 wires them to real data and validates the end-to-end delta loop.

1. ~~`app/pipeline/deduplicator.py`~~ ✅ Built in Phase 1 — wire to real connector output
2. ~~Worker: delete old chunks before re-embedding~~ ✅ Built in Phase 1 — validate with real data
3. ~~Worker: accumulate stats in memory~~ ✅ Built in Phase 1 (`JobStats`, `JobLifecycleService`)
4. ~~`DocumentRepository.upsert()`~~ ✅ Built in Phase 1 — validate round-trip
5. ~~Write final stats to `ingestion_jobs`~~ ✅ Built in Phase 1 — validate correct counts
6. ~~Local connector: yield `DeltaItem`~~ ✅ Built in Phase 1 — validate second-run skip behaviour
7. SharePoint connector: `lastModifiedDateTime` for delta (full delta API in Phase 6)
8. **Phase 4 focus:** Run real end-to-end test — ingest folder, modify file, ingest again, verify correct new/updated/skipped counts

**Acceptance:** Run ingestion twice on same local folder. Second run: 0 new, N skipped. Modify one file. Third run: 1 updated, N-1 skipped.

---

### 📦 Phase 5 — Scheduling System
**Goal:** Repos auto-trigger on their configured schedule.

**Deliverables:**
1. APScheduler 4.x integrated into orchestrator lifespan
2. `app/services/scheduler_service.py` — register/remove/pause/resume
3. On orchestrator startup: load all active scheduled repos, register jobs
4. `PUT /api/v1/repos/{id}/schedule` — update schedule, takes effect immediately
5. `POST /api/v1/repos/{id}/schedule/pause` and `/resume`
6. `coalesce=True` and `misfire_grace_time` configured

**Acceptance:** Set repo to 1-minute interval. Observe jobs being triggered every minute. Update to 2-minute interval via API — takes effect without restart. Pause — no more triggers.

---

### 📦 Phase 6 — Production SharePoint Connector
**Goal:** DocAssist SharePoint connector ready for real data.

**Deliverables:**
1. Full `SharePointConnector` rewrite with:
   - Graph SDK authentication
   - Delta API (`/drive/root/delta`) with token persistence
   - Recursive folder traversal + `full_path` tracking
   - Pagination via `@odata.nextLink`
   - File size + extension filtering
   - Hidden file skip (`~$` prefix)
   - Per-file error isolation
   - `source_url` populated from Graph item `webUrl`
2. `DeltaToken` saved to `source_repositories.state.delta_token` after successful run
3. Deleted items handled

**Acceptance:** Run against real DocAssist SharePoint. Full scan completes. Second run uses delta API and processes only changed files.

---

### 📦 Phase 6b — SQL & MongoDB Connectors
**Goal:** Ingest structured data from SQL databases and MongoDB collections.

**Why here:** All connector implementations belong together — SharePoint, SQL, and MongoDB are all "how we fetch data". Architecture is already ready: `content_type="structured"` and `StructuredFormatter` were built proactively.

**SQL Connector (`connectors/sql.py`):**

Config keys in `connector_config`:
```json
{
  "db_type": "postgresql",
  "tables": [
    {
      "table_name": "customers",
      "primary_key": "customer_id",
      "delta_column": "updated_at",
      "column_descriptions": {
        "annual_revenue": "Total annual revenue in USD",
        "region": "Geographic sales region"
      },
      "exclude_columns": ["password_hash", "internal_notes"]
    }
  ],
  "batch_size": 500
}
```

Credentials:
```json
{ "connection_string": "postgresql+asyncpg://user:pass@host/db" }
```

Delta strategy:
- `delta_column` present → `WHERE updated_at > last_run_at`
- No `delta_column` → full re-scan, hash comparison via deduplicator
- Deleted rows → compare seen primary keys vs registry, missing = deleted

Pipeline path: row → `StructuredFormatter.from_sql_row()` → `RawDocument(content_type="structured")` → chunker (loader skipped)

**MongoDB Connector (`connectors/mongodb.py`):**

Config keys in `connector_config`:
```json
{
  "database": "mydb",
  "collections": [
    {
      "collection_name": "orders",
      "id_field": "_id",
      "delta_field": "updated_at",
      "filter_query": { "status": { "$ne": "draft" } },
      "field_descriptions": {
        "total": "Order total in USD",
        "status": "Current fulfillment status"
      },
      "skip_fields": ["internal_log", "raw_payload"]
    }
  ],
  "batch_size": 200
}
```

Credentials:
```json
{ "connection_string": "mongodb+srv://user:pass@cluster.mongodb.net" }
```

Delta strategy:
- `delta_field` present → `{ delta_field: { $gt: last_run_at } }`
- No `delta_field` → full re-scan with content hash comparison
- Uses `pymongo.AsyncMongoClient` — already in requirements, zero new dependency

Pipeline path: document → `StructuredFormatter.from_mongo_document()` → `RawDocument(content_type="structured")` → chunker (loader skipped)

**Deliverables:**
1. `app/connectors/sql.py` — `SQLConnector(BaseConnector)` supporting PostgreSQL and MySQL
2. `app/connectors/mongodb.py` — `MongoDBConnector(BaseConnector)` using existing pymongo
3. `app/connectors/factory.py` — register `"sql"` and `"mongodb"` source types
4. `requirements.txt` (worker) — add `asyncpg>=0.30.0`, `aiomysql>=0.2.0`
5. End-to-end test: SQL table + MongoDB collection ingested, delta on second run works

**New dependencies (worker only):**
- `asyncpg>=0.30.0` — async PostgreSQL driver
- `aiomysql>=0.2.0` — async MySQL driver
- `pymongo` — already present, MongoDB connector needs no new package

**Acceptance:** Register a SQL repo via API. Trigger ingestion. Worker fetches rows, formats as Markdown, chunks, embeds, stores. Second run: only changed rows re-ingested. Same for MongoDB.

---

### 📦 Phase 7 — Enhanced Loaders & Provider Abstractions
**Goal:** Tables and images handled correctly. Embedding and vector store are swappable.

**Deliverables:**
1. ~~`BaseEmbeddingProvider` + `OpenAIEmbeddingProvider`~~ ✅ Built in Phase 1 (uses `langchain-openai` internally)
2. ~~`BaseVectorStore` + `MongoDBAtlasVectorStore`~~ ✅ Built in Phase 1 (uses `langchain-mongodb`)
3. Provider selection at worker startup based on tenant config
4. `DoclingPDFLoader` — `TableFormerMode.ACCURATE`, OCR, image description via VLM
5. `EnhancedDocxLoader` — MarkItDown + python-docx image extraction + VLM
6. `PptxLoader` — slide-aware with slide_number in metadata
7. `MetadataEnricher` with standard metadata + tenant custom fields
8. Per-tenant vector index provisioning on repo creation

**Acceptance:** Ingest a PDF with tables — verify tables appear as proper Markdown tables in stored chunks. Ingest a Word doc with an image — verify `[IMAGE: description]` appears in chunk. Check chunk metadata has `full_path`, `source_url`, `page_start`, `section_headers`.

---

### 📦 Phase 8 — Security Hardening & Rate Limiting
**Goal:** API secured. Credentials properly isolated.

**Deliverables:**
1. API key auth middleware (`X-API-Key`)
2. `api_keys` collection + `GET /api/v1/auth/keys` management
3. `get_current_tenant` dependency injected on all protected routes
4. Rate limiting (slowapi) — 10 req/min for triggers, 60 req/min for reads
5. Credential scrubbing structlog processor
6. Tenant ownership validation (tenant A cannot access repo B that belongs to tenant C)
7. Redis AUTH configured in settings

**Acceptance:** Unauthenticated request → 401. Wrong tenant's repo → 403. Trigger flood → 429 after 10 requests.

---

### 📦 Phase 9 — Batch Embedding & Document Concurrency
**Goal:** High-throughput embedding with concurrency control.

**Deliverables:**
1. `asyncio.TaskGroup` for document-level concurrency in worker
2. `asyncio.Semaphore` based on `repo_config.document_concurrency`
3. Embedding provider batches chunks, concurrent batch calls with semaphore
4. Per-tenant concurrency config resolved from tenant defaults + repo overrides
5. Memory profiling: verify no OOM for large repos

**Acceptance:** Ingest 100-document folder. Observe via logs that 10 documents process concurrently. OpenAI calls are batched, not 1-per-chunk.

---

## 21. Progress Tracker

> Update this section after completing each phase. Include date and any key decisions made during implementation.

| Phase | Status | Completed | Notes |
|-------|--------|-----------|-------|
| 1 — Foundation & Bug Fixes (Orchestrator) | ✅ Done | Feb 24 2026 | All 5 original bugs fixed. Health endpoints tested and working. |
| 1 — Foundation & Bug Fixes (Worker) | ✅ Done | Feb 24 2026 | Full worker skeleton with all abstractions in place. See notes below. |
| 2 — Repository Pattern | ✅ Done | Feb 24 2026 | Included in Phase 1 — repos built alongside the services from the start. |
| 3 — Tenant & Repo Management API | ✅ Done | Feb 26 2026 | Full CRUD for tenants + repos. Ingest trigger moved to repos.py. Discriminated union on source_type. tenant_id caller-supplied (see decisions log). Worker fully compatible — no changes needed. |
| 4 — Delta Ingestion | ✅ Done | Feb 26 2026 | All 4 runs validated: fresh ingest, no-change skip, update (hash change), file deletion. 2 pre-flight bugs fixed. |
| 5 — Scheduling | ✅ Done | Feb 26 2026 | APScheduler 4.x wired. GET/PUT/pause/resume schedule endpoints. Startup loads all active scheduled repos. Note: 4.x uses AsyncScheduler + add_schedule() — completely different API from 3.x. |
| 6 — Production SharePoint Connector | ✅ Done | Mar 04 2026 | Full Graph API connector. OAuth2 client credentials. Full + delta scan. Pagination, filtering, hidden file skip, per-file error isolation. delta_token persisted via repo state. Worker passes delta_token to factory. |
| 6b — SQL & MongoDB Connectors | ✅ Done | Mar 16 2026 | SQLConnector (PostgreSQL+MySQL via SQLAlchemy async, batch pagination, delta via updated_at). MongoDBConnector (pymongo async, filter_query, delta via delta_field). Both loaders produce Markdown tables/sections. last_run_at read from state + saved after each run. factory wired with last_run_at injection. |
| 7 — Enhanced Loaders & Providers | ✅ Done | Mar 15 2026 | PptxLoader (slide-aware, tables, notes, Vision images). MetadataEnricher custom fields (path_segment, file_name, static). Per-tenant embedding model selection from ingestion_defaults. EMBEDDING_MODEL added to settings. |
| 8 — Security Hardening (Slimmed) | ✅ Done | Mar 16 2026 | Credential scrubbing already in place (logging.py). Cross-tenant repo ownership via verify_repo_ownership() using tenant_id query param (Ocelot handles auth, tenant_id trusted from path/param). REDIS_PASSWORD optional in both services. API key auth skipped — MS Entra + Ocelot handles it. |
| 9 — Batch Embedding & Concurrency | ✅ Done | Mar 16 2026 | asyncio.TaskGroup + Semaphore for document concurrency (default 10). OpenAIEmbeddingProvider: explicit batch_size + concurrent batch calls via shared Semaphore at worker level. document_concurrency / embedding_batch_size / embedding_concurrency read from tenant ingestion_defaults. |
| R5 — Ingestion Quality (TableConverter + ChunkContextualiser) | ✅ Done | Mar 22 2026 | TableConverter: pipe tables → prose rows with preceding_context (heading fallback). ChunkContextualiser (R6b): sliding window LLM context, 2-sentence semantic summary per chunk. |
| W1 — Credentials Update | ✅ Done | Mar 22 2026 | PATCH /repos/{repo_id}/credentials endpoint. Source-type validation. Fernet encrypted storage. |
| W2/W3 — Notifications | ✅ Done | Mar 22 2026 | SendGrid email + webhook. 6 auth types (NoneAuth, ApiKey, Bearer, CustomHeaders, HMAC, OAuth2 with Entra/Apigee). JobStats tracks document lists per result. |
| XLSX — Excel Multi-Sheet Ingestion | ✅ Done | Mar 24 2026 | XlsxLoader: header detection, merged cells, hidden sheet skip, links preserved, Excel-style column naming. |
| MCP Server | ✅ Done | Mar 26 2026 | Registry+proxy pattern. 3 tools: search_documents, query_knowledge_base, list_repos. stateless_http=True. Dynamic tenant activate/deactivate. In-memory registry. |
| R6a — Document Identity Prefix | ✅ Done | Mar 28 2026 | DocumentPrefixBuilder: zero hardcoded fields, tenant-driven via extractable_fields → filterable_fields → all custom. Format: [App | Domain | FileStem | Heading]. |
| R6d — Hybrid Search | ✅ Done | Mar 28 2026 | $vectorSearch + $search + $rankFusion in mongodb_atlas.py. hybrid_search_enabled + hybrid_alpha in retrieval_config (generic). search_index_name in vector_config (Atlas-specific). |
| R6e — Reranker | ✅ Done | Mar 30 2026 | Reranker service using tenant LLM. vector_top_k = min(max(top_k*3,20),30). MAX_RERANK_CHARS=400 (captures R6a+R6b context). One LLM call for all candidates. Graceful degradation. |
| SharePoint Delta Fix | ✅ Done | Mar 29 2026 | Fixed: item.get("deleted") is not None (not @removed). Root-level delta URL. Root folder path filter. |
| C1 — OneDrive Connector | ✅ Done | Mar 30 2026 | Graph API delta, same pattern as SharePoint. Drive discovery from user_id. supports_native_deletes=True. 410 handling for expired tokens. Retry on 429. |
| C2 — Azure Blob Connector | ✅ Done | Mar 30 2026 | azure-storage-blob[aio]>=12.24.0. Three auth modes: connection_string, shared_key, DefaultAzureCredential (Managed Identity). Delta via last_modified timestamp. supports_native_deletes=False (worker Step 5 registry diff). |
| v10 — must/should filters | ✅ Done | Apr 20 2026 | SearchFilters: must_filters (hard boundary) + should_filters (soft scope) + auto_extract (bool). filter_builder priority chain. retrieval_service auto_extract gating. filter_extractor uses should_filters as known_values scope. |
| v10 — repo-level metadata_schema | ✅ Done | Apr 20 2026 | metadata_schema on CreateRepoRequest. Worker resolves: repo > tenant priority. metadata_overrides REMOVED from IngestionOverrides. _resolve_effective_schema() removed from enricher. |
| v10 — computed field | ✅ Done | Apr 20 2026 | New source="computed" in CustomFieldSchema. formula="{application}::{access_group}" → "SPT::general". Enables per-app access_key. computed fields must be last in custom_fields list. |

### Requirements.txt Locations

```
ingestion-orchestrator/requirements.txt    ← orchestrator dependencies
ingestion-worker/requirements.txt          ← worker dependencies
retrieval-service/
├── main.py
├── requirements.txt
├── .env.example
└── app/
    ├── core/              config, logging, exceptions, encryption, api_config
    ├── db/                AsyncMongoClient
    ├── repositories/      base, tenant_repo, repo_repo
    ├── schemas/           search (SearchFilters), requests, responses
    ├── providers/
    │   ├── embedding/     base, openai_provider, factory
    │   └── vectorstore/   base, mongodb_atlas, factory
    ├── services/          filter_builder, retrieval_service, llm_service
    └── api/               health, routes, dependencies
```

Key dependency notes:
- `cryptography>=42.0.0` — in both orchestrator and worker for Fernet encryption
- `azure-keyvault-secrets` + `azure-identity` — commented out, uncomment for AKS Key Vault
- `azure-identity` in worker is already active for SharePoint Graph API auth
- `aiohttp>=3.9.0` — added to worker for SharePoint connector HTTP calls

### Post-Phase Additions

These changes were made after the main phase plan completed — improvements and fixes
discovered during testing and architecture refinement.

#### Retrieval Service Architecture (Mar 17 2026)
Full architecture planned for `docassist-retrieval` microservice.
See Section 20 below for complete specification.

#### Credential Encryption — Repo connector_config (Mar 17 2026)
**Problem:** SharePoint `client_secret`, SQL/MongoDB `connection_string` stored plaintext.

**Solution:** `SENSITIVE_CREDENTIAL_FIELDS` registry in `encryption.py` — hardcoded
per connector type since sensitivity is not tenant-configurable.

```python
SENSITIVE_CREDENTIAL_FIELDS = {
    "sharepoint": {"client_secret"},
    "sql":        {"connection_string", "password"},
    "mongodb":    {"connection_string", "mongo_uri", "password"},
    "local":      set(),
}
```

Flow:
- Orchestrator `RepoService.create_repo()` → `encrypt_credentials()` → stores ciphertext
- Worker `ConnectorFactory.get_connector()` → now **async** → `decrypt_credentials()` → plaintext to connector
- Worker `worker.py` → `await get_connector()`

**Files changed:**
- `app/core/encryption.py` (both) — `encrypt_credentials()` + `async decrypt_credentials()` added
- `app/services/repo_service.py` (orchestrator) — `encrypt_credentials()` on create
- `app/connectors/factory.py` (worker) — async + `decrypt_credentials()` before connector build
- `app/worker.py` (worker) — `await get_connector()`

#### RetrievalConfig on Repo Schema (Mar 17 2026)
**Problem:** Cross-repo queries with mismatched metadata filters silently miss documents
(e.g. `application="AppA"` filter applied to a SQL repo that has no `application` field).

**Solution:** `retrieval_config` field on every repo — tells retrieval service which
metadata fields are filterable for THIS repo.

```json
{
  "retrieval_config": {
    "filterable_fields": ["domain", "application", "is_general"],
    "general_flag_field": "is_general",
    "general_flag_value": "true"
  }
}
```

Retrieval service reads each repo's `retrieval_config` and only applies filters
that the repo actually supports. Unsupported filter fields are silently skipped
per repo — enabling safe cross-repo search with tenant-level filters.

**Files changed:**
- `app/schemas/repo_config.py` (orchestrator) — `RetrievalConfig` model + added to Create/Patch/Response
- `app/services/repo_service.py` (orchestrator) — `retrieval_config` passed through
- `app/repositories/repo_config_repo.py` (orchestrator) — `retrieval_config` stored in MongoDB

#### Worker Logging Fix — SafeStreamHandler (Mar 17 2026)
**Problem:** `TypeError: 'str' object does not support item deletion` from
`structlog.stdlib.ProcessorFormatter.remove_processors_meta` when third-party
libraries (arq, langchain internals) emit malformed log records.

**Solution:** `_safe_remove_processors_meta` — replaces standard processor,
checks `isinstance(event_dict, dict)` before deletion. Also added suppressions
for `langchain_*`, `azure.*`, `msal`, `httpcore`, `urllib3` loggers.

**File changed:**
- `app/core/logging.py` (worker)

#### Tenant genai_api_key Encryption (Mar 17 2026)
**Problem:** `genai_api_key` was stored in plaintext in MongoDB — security risk if
database backup is stolen.

**Solution:** Fernet AES encryption (local/dev) with Azure Key Vault hook (production/AKS).

**Mode detection — automatic from settings:**
```
LOCAL DEV  (SECRET_ENCRYPTION_KEY set, KEY_VAULT_URL not set):
  encrypt() → Fernet encrypt → ciphertext stored in MongoDB
  decrypt() → Fernet decrypt → plaintext at runtime

PRODUCTION (KEY_VAULT_URL set — AKS Managed Identity):
  encrypt() → store Key Vault secret NAME as-is
  decrypt() → fetch secret VALUE from Key Vault (no credentials needed)

PASSTHROUGH (neither set — dev without real secrets):
  encrypt/decrypt → value stored/returned as-is + warning logged
```

**Security properties:**
- MongoDB dump stolen → encrypted ciphertext useless without SECRET_ENCRYPTION_KEY ✅
- API response → genai_api_key always returned as "***REDACTED***" ✅
- Redis queue → credentials never in queue (established Phase 1) ✅
- Logs → _scrub_sensitive_data processor redacts key fields ✅

**AKS migration path (zero code changes):**
1. Add `KEY_VAULT_URL` to k8s secret
2. Pre-create secret: `az keyvault secret set --name kv-docassist-key --value "sk-..."`
3. PATCH tenant: `{ "api_config": { "genai_api_key": "kv-docassist-key" } }`
4. Uncomment `azure-keyvault-secrets` in requirements.txt

**Files changed:**
- `app/core/encryption.py` (orchestrator + worker) — NEW: encrypt_value(), decrypt_value()
- `app/core/config.py` (both) — SECRET_ENCRYPTION_KEY + KEY_VAULT_URL settings added
- `app/schemas/tenant.py` (orchestrator) — genai_api_key masked in API response
- `app/services/tenant_service.py` (orchestrator) — encrypt on create/patch
- `app/core/api_config.py` (worker) — resolve_api_config() now async, calls decrypt_value()
- `app/worker.py` (worker) — await resolve_api_config()
- `requirements.txt` (both) — cryptography>=42.0.0 added, azure libs commented for later

#### Per-Tenant ApiConfig (Mar 17 2026)
**Problem:** All tenants shared the same global OpenAI API key — no cost isolation,
no support for tenants on different Azure OpenAI endpoints.

**Solution:** `api_config` field added to tenant schema:
```json
{
  "api_config": {
    "genai_api_key":  "sk-...",    // null = use global settings.OPENAI_API_KEY
    "genai_base_url": "https://genai-pwc.../",  // null = use OpenAI default
    "embedding_model": null,        // null = use ingestion_defaults.embedding_model
    "llm_model":       "gpt-4o"    // null = use global settings.LLM_MODEL
  }
}
```
Resolution priority: `tenant api_config` → `tenant ingestion_defaults` → `global settings`

`resolve_api_config()` utility in `app/core/api_config.py` shared by both
ingestion worker and retrieval service (copy to retrieval service on build).

**Files changed:**
- `app/schemas/tenant.py` (orchestrator) — `ApiConfig` model + added to Create/Patch/Response
- `app/services/tenant_service.py` (orchestrator) — passes api_config through create/patch
- `app/repositories/tenant_repo.py` (orchestrator) — stores api_config in MongoDB
- `app/core/api_config.py` (worker) — NEW: `resolve_api_config()` resolver utility
- `app/core/config.py` (worker) — `LLM_MODEL` setting added (default gpt-4o)
- `app/providers/embedding/openai_provider.py` (worker) — `base_url` param added
- `app/worker.py` (worker) — uses `resolve_api_config()` for per-tenant embedding provider

#### MetadataEnricher — path_segment_equals source (Mar 16 2026)
**Problem:** No way to mark "general" folder files as always-include regardless of
application filter. Needed for DocAssist folder structure:
`domain1/AppA/file.pdf`, `domain2/AppC/file.pdf`, `general/policy.pdf`

**Solution:** New `path_segment_equals` source type in `CustomFieldSchema`:
```json
{
  "field_name": "is_general",
  "source": "path_segment_equals",
  "path_segment_index": 0,
  "match_value": "general",
  "true_value": "true",
  "false_value": "false"
}
```
Enables retrieval filter: `application = "AppA" OR is_general = "true"`

**Files changed:**
- `app/schemas/tenant.py` (orchestrator) — `CustomFieldSchema` updated with new fields
- `app/pipeline/metadata_enricher.py` (worker) — `path_segment_equals` source implemented

#### Security — Simplified to cross-tenant ownership check (Mar 16 2026)
**Decision:** tenant_id comes as API path/query param (not in Entra token claims).
Ocelot handles authentication. Orchestrator only needs to prevent cross-tenant
repo access (tenant A accessing repo belonging to tenant B).

**Implementation:** `verify_repo_ownership(repo_id, repo.tenant_id, tenant_id_param)`
called on all routes that take `repo_id` without `tenant_id` in path.
`tenant_id` added as required query param on these routes.

**Files changed:**
- `app/core/security.py` (orchestrator) — simplified to single `verify_repo_ownership()`
- `app/api/repos.py` (orchestrator) — `?tenant_id=` query param on repo-id-only routes
- `app/api/schedules.py` (orchestrator) — same pattern
- `app/api/jobs.py` (orchestrator) — ownership check on `/repos/{id}/jobs`

### Phase 1 Completion Notes

**Orchestrator — files built:**
- `requirements.txt` — pinned dependencies
- `app/core/config.py` — pydantic-settings, REDIS_HOST/PORT as computed fields
- `app/core/logging.py` — structlog with stdlib bridge (fixed PrintLogger bug)
- `app/core/exceptions.py` — full exception hierarchy
- `app/db/mongo.py` — AsyncMongoClient singleton, connect/close/get pattern
- `app/db/indexes.py` — all collection indexes, idempotent
- `app/queue/producer.py` — ARQ pool with RedisSettings.from_dsn()
- `app/repositories/base.py` — BaseRepository ABC
- `app/repositories/job_repo.py` — create, get, list, active-check
- `app/services/job_service.py` — 409 guard, queue payload = IDs only
- `app/schemas/job.py` — request/response models
- `app/api/dependencies.py` — full DI chain via FastAPI Depends()
- `app/api/health.py` — /health/live + /health/ready (probes MongoDB + Redis)
- `app/api/jobs.py` — trigger, status, history endpoints (Bug B5 fixed)
- `main.py` — lifespan, both Swagger + Scalar UI
- `.env.example`, `Dockerfile`, `docker-compose.yml` (Redis only), `.gitignore`

**Worker — files built:**
- `requirements.txt` — includes docling, markitdown, openai, msgraph-sdk
- `app/core/` — config (adds OPENAI_API_KEY), logging, exceptions (worker-specific hierarchy)
- `app/db/` — identical pattern to orchestrator
- `app/repositories/` — JobRepo (update-only), DocumentRepo (delta registry), RepoConfigRepo (load config + credentials)
- `app/connectors/base.py` — RawDocument, DeltaItem, BaseConnector ABC
- `app/connectors/local.py` — fully working, async file reads, hash-based dedup, per-file error isolation
- `app/connectors/sharepoint.py` — stub, validates credentials, fetch_documents() is empty (Phase 6)
- `app/connectors/factory.py` — registry pattern, add connector = register one line
- `app/loaders/base.py` — BaseLoader ABC, ParsedDocument dataclass
- `app/loaders/text_loader.py` — fully working
- `app/loaders/pdf_loader.py` — Docling, asyncio.to_thread() (Phase 7 adds tables/OCR)
- `app/loaders/docx_loader.py` — MarkItDown (Phase 7 adds image extraction)
- `app/loaders/factory.py` — strict: raises UnsupportedDocumentTypeError (no silent fallback)
- `app/providers/embedding/base.py` — BaseEmbeddingProvider ABC
- `app/providers/embedding/openai_provider.py` — batching + tenacity retry
- `app/providers/vectorstore/base.py` — BaseVectorStore ABC (add_documents, delete_by_source)
- `app/providers/vectorstore/mongodb_atlas.py` — uses aadd_documents, instantiated once per task
- `app/pipeline/chunker.py` — two-stage: MarkdownHeader → RecursiveCharacter
- `app/pipeline/metadata_enricher.py` — standard metadata + Phase 7 hook for custom fields
- `app/pipeline/deduplicator.py` — new/changed/unchanged decisions
- `app/services/ingestion_service.py` — full 8-step pipeline per document
- `app/services/job_lifecycle.py` — in-memory stats accumulator, single DB write at end
- `app/worker.py` — ARQ WorkerSettings, startup/shutdown hooks, process_ingestion_task

**Known stubs (to be completed in their respective phases):**
- SharePointConnector.fetch_documents() — Phase 6
- PDFLoader table/OCR/image features — Phase 7
- DocxLoader image extraction + VLM — Phase 7
- MetadataEnricher custom tenant fields — Phase 7
- APScheduler integration — Phase 5
- API key auth middleware — Phase 8
- Concurrent document processing (TaskGroup + Semaphore) — Phase 9

**How to test end-to-end right now (local files):**
1. Insert test tenant + repo directly into MongoDB Atlas (see orchestrator README)
2. Start Redis: `docker-compose up -d` (in orchestrator folder)
3. Start orchestrator: `uvicorn main:app --reload --port 8000`
4. Start worker: `arq app.worker.WorkerSettings`
5. Trigger: `POST http://localhost:8000/api/v1/repos/repo_local_test/ingest`
6. Watch worker logs — full pipeline output per document
7. Check job status: `GET http://localhost:8000/api/v1/jobs/{job_id}`

**Next step:** Phase 4 — Run the 4-run validation test sequence (see Phase 4 section in coding plan for exact steps)
This eliminates the manual MongoDB insert step above.
Endpoints to build: POST /tenants, POST /tenants/{id}/repos, GET/PATCH/DELETE repos.

---

## Appendix A — Key Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| Feb 2026 | `shared_tenant` as default index mode | Simpler for DocAssist (one tenant, one index), easily migrated to `dedicated` per repo if needed |
| Feb 2026 | ARQ over Celery | Already in use, pure async, simpler worker model for Python 3.13 |
| Feb 2026 | APScheduler 4.x over Celery Beat | Embedded in FastAPI, no extra process, async-native, dynamic job management |
| Feb 2026 | Repository pattern | Makes DB swappable, services testable without mocking raw collections |
| Feb 2026 | Provider ABC for vectorstore + embedding | Future-proof: swap Atlas → Pinecone or OpenAI → Azure OpenAI = add one class + change config |
| Feb 2026 | Stats accumulated in-memory during job | Avoid one MongoDB write per document; single write at job completion |
| Feb 2026 | Credentials NOT in Redis queue | Security — queue payload is safe to log; credentials never leave DB |
| Feb 2026 | Repository pattern built in Phase 1, not Phase 2 | Made more sense to build repos alongside services from the start — Phase 2 effectively merged into Phase 1 |
| Feb 2026 | LoaderFactory raises UnsupportedDocumentTypeError (no silent fallback) | Original code silently fell back to TextLoader for unknown MIME types — this hid bugs. Now explicit. |
| Feb 2026 | Use LangChain for embedding & vector store internals | langchain-openai OpenAIEmbeddings and langchain-mongodb MongoDBAtlasVectorSearch are stable, well-maintained, and handle batching/Atlas query format correctly. Our BaseEmbeddingProvider and BaseVectorStore ABCs wrap them — rest of system never imports LangChain directly. Retrieval service gets similarity_search() for free. |
| Feb 2026 | IngestionService instantiated per-task, LoaderFactory instantiated per-worker | LoaderFactory loads ML models (Docling, MarkItDown) once at worker startup. IngestionService is per-task because it needs the repo-specific index name. |
| Feb 2026 | structlog stdlib bridge (ProcessorFormatter) not PrintLoggerFactory | PrintLoggerFactory + add_logger_name caused AttributeError in production. stdlib LoggerFactory gives proper .name attribute and also captures FastAPI/uvicorn logs in same format. |
| Feb 2026 | Docling replaced by pymupdf4llm + ChatOpenAI Vision | Docling requires ~1.5GB HuggingFace model downloads at runtime — blocked by corporate firewall/security policy (PwC environment). pymupdf4llm is pure C, zero internet dependencies. Scanned PDFs and images handled by ChatOpenAI Vision via already-approved LangChain endpoint — only called when complexity detected. |
| Feb 2026 | Two-stage loader pattern (library-first, LLM-only-if-needed) | Complexity detected BEFORE any LLM call. PDF: pymupdf char-count per page. DOCX: python-docx inline_shapes + cell.tables XML check. LLM only invoked for scanned content or images — not for every document. Controls token cost and latency. |
| Feb 2026 | SQL connector uses LIMIT/OFFSET pagination, batch_size=500 | Avoids loading entire table into memory. Primary key + delta_column always included in SELECT even when columns_include is configured, to ensure source_id and delta detection always work. |
| Feb 2026 | Structured RawDocument uses raw_data=dict not content=bytes | SQL/MongoDB connectors pass raw row/document as a Python dict via raw_data field. The loader (SQLLoader/MongoDBLoader) converts to Markdown. Old stubs incorrectly used content=json_bytes + mime_type="application/json" which bypassed structured loader routing entirely and broke the LoaderFactory. |
| Feb 2026 | Connector files named *_connector.py | Avoids Python import shadowing (e.g. "mongodb" could shadow pymongo internals). Factory imports by full module name. Consistent naming convention across all connectors. |
| Mar 2026 | Credential encryption hardcoded per connector type (not tenant-configurable) | A client_secret is always sensitive regardless of tenant preference. Hardcoding avoids extra schema complexity and configuration burden. Per-connector registry in SENSITIVE_CREDENTIAL_FIELDS is easy to extend when new connectors are added. |
| Mar 2026 | get_connector() is async — decryption happens in factory not worker | Factory already knows source_type (needed for SENSITIVE_CREDENTIAL_FIELDS lookup). Connector stays auth-agnostic — receives plaintext credentials. Worker stays clean — just awaits factory. |
| Mar 2026 | retrieval_config per repo not global | Cross-repo queries with global filters silently miss documents on repos that don't have those metadata fields. Per-repo filterable_fields makes this explicit and safe. |
| Mar 2026 | SSE transport → Streamable HTTP for MCP | SSE deprecated effective April 1 2026 by MCP spec. Streamable HTTP is stateless — enables multiple server instances behind load balancer, no reconnection issues, cloud-native. fastmcp v3.1.1 supports both but Streamable HTTP is the standard going forward. |
| Mar 2026 | One FastMCP instance per tenant at /mcp/{tenant_id} | Tools are pre-scoped to tenant at mount time — no tenant_id parameter needed in tool calls. LLM gets cleaner tool descriptions. Ocelot can route per-tenant cleanly. Tenants are loaded at startup from active MongoDB records. |
| Mar 2026 | OPENAI_API_VERSION as explicit setting | Azure OpenAI requires API version in every request. langchain-openai reads it from env var automatically. Making it explicit in config allows per-deployment override without code changes. Default: 2024-08-01-preview. |
| Mar 2026 | encryption.py and api_config.py copied not shared | Both files are self-contained with no cross-service imports. Copying is simpler than a shared library — avoids packaging overhead, keeps each service independently deployable. Same key/logic guaranteed by copying, not by reference. |
| Mar 2026 | tenant_id in URL path for retrieval service | Ocelot can enforce tenant routing without touching request body. URL itself is the tenant identity — clean, auditable, RESTful. |
| Mar 2026 | Fernet encryption now, Key Vault hook built-in for later | Building the Key Vault conditional branch now costs ~10 lines. Without it, migrating to Key Vault later requires changing how genai_api_key is stored (ciphertext → secret name), updating the resolver, and retesting. The hook makes the migration zero-code-change on the application side. |
| Mar 2026 | resolve_api_config() is async because decryption may be async (Key Vault) | Fernet decrypt is sync but Key Vault fetch is async (network call). Making the function async from the start avoids a breaking change later. Worker awaits it — no performance impact since it runs once per task. |
| Mar 2026 | genai_api_key always REDACTED in API responses | Even the encrypted ciphertext is not returned — it could leak the encryption scheme. API responses show "***REDACTED***". The actual value is only accessible at runtime inside the service process. |
| Mar 2026 | Per-tenant ApiConfig: one key covers embedding + LLM (not two) | Two keys adds operational complexity (double rotation, double Key Vault entries) with no real benefit. OpenAI already separates costs by model in their dashboard. One key per tenant is enough for cost isolation. Two-key option deferred — can add llm_api_key separately if needed. |
| Mar 2026 | resolve_api_config() shared utility — single source of truth for provider resolution | Both ingestion worker and retrieval service need identical resolution logic. Single file copied to both services. Priority chain: tenant api_config → tenant ingestion_defaults → global settings. Guaranteed non-null output via ResolvedApiConfig dataclass. |
| Mar 2026 | base_url on OpenAIEmbeddingProvider enables Azure OpenAI per tenant | Different tenants may be on different Azure OpenAI deployments (US, EU) for data residency. base_url=None falls back to OpenAI default. Passed via openai_api_base kwarg to LangChain OpenAIEmbeddings. |
| Mar 2026 | path_segment_equals source type for is_general flag | retrieval needs: WHERE application="AppA" OR is_general="true". Boolean flag stored as string "true"/"false" (MongoDB Atlas vector search pre-filters work with string equality). Configurable true_value/false_value for flexibility. |
| Mar 2026 | tenant_id as query param on repo-id-only routes for ownership check | tenant_id not in Entra token, comes from client. Routes with {tenant_id} in path are self-contained. Routes with only {repo_id} need ?tenant_id= to verify the repo belongs to the caller. Simple, no header dependency. |
| Mar 2026 | API key auth skipped — MS Entra + Ocelot handles authentication | Building API key middleware on top of an already-authenticated request (Entra token → Ocelot → service) is redundant. Ocelot handles auth, rate limiting, and token validation. We focus on what Ocelot cannot know: internal tenant ownership rules. |
| Mar 2026 | X-Tenant-Id header trusted from Ocelot, None = dev mode | Ocelot forwards authenticated tenant as X-Tenant-Id header from the Entra token claims. If header is absent (direct API call / dev mode), ownership checks are skipped. This means dev workflow is unchanged. Production is protected. |
| Mar 2026 | Ownership checks in route layer, not service layer | verify_repo_ownership() and verify_tenant_access() called in route handlers before any service call. Services stay auth-agnostic and remain unit-testable without security context. |
| Mar 2026 | asyncio.TaskGroup streams connector output and dispatches upserts concurrently | TaskGroup is opened around the connector.fetch_documents() loop. Deleted items handled synchronously inline (no concurrency needed). Upsert items dispatched as tasks immediately — fetch and process overlap. TaskGroup.exit awaits all tasks before Step 5 runs. |
| Mar 2026 | Semaphore per task not per worker process | Each ingestion task gets its own asyncio.Semaphore(document_concurrency). This means two simultaneous ingestion jobs for different repos each get their own full concurrency budget. A process-level semaphore would starve one job when two run at once. |
| Mar 2026 | Embedding provider batching: split → concurrent TaskGroup → flatten | texts split into batch_size chunks. All batches launched concurrently via TaskGroup bounded by Semaphore. Results list pre-allocated by index so order is preserved without sorting. Single-batch shortcut skips TaskGroup overhead for small documents. |
| Mar 2026 | deduplicator exposed as property not accessed via _deduplicator | Worker accesses deduplicator for deleted item handling and implicit delete detection. Property keeps the interface clean and avoids name-mangled private access from outside the class. |
| Mar 2026 | last_run_at injected into SQL/MongoDB connectors via factory, not constructor | Connectors need last_run_at for delta fetch but it's only known at task time (from repo state). Factory reads it from state and injects post-construction via connector.last_run_at = value. Keeps connector __init__ clean and consistent with SharePoint's delta_token pattern. |
| Mar 2026 | last_successful_run_at saved per-run only for sql/mongodb source types | SharePoint uses delta_token (Graph API native). Local connector has no delta state. Only sql/mongodb need timestamp-based delta. Worker branches on source_type after completion. |
| Mar 2026 | PptxLoader uses python-pptx directly, not MarkItDown | MarkItDown merges all slides into one flat block — slide boundaries and speaker notes are lost. python-pptx gives per-slide control: ## Slide N headers, speaker notes as blockquotes, tables as Markdown tables. Vision images replace [IMAGE_PLACEHOLDER] markers after sync extraction. |
| Mar 2026 | MetadataEnricher custom fields: path_segment/file_name/static sources | Three extraction strategies cover 95% of tenant needs. path_segment extracts folder hierarchy (e.g. /finance/Q3.pdf → "finance"). file_name uses the stem. static applies the same value to all docs. Extensible — add new source types without changing the enricher contract. |
| Mar 2026 | Per-tenant embedding model from ingestion_defaults.embedding_model | Worker reads tenant config at task start, creates a task-scoped provider if model differs from global default. Falls back to startup provider if override fails. Zero change to orchestrator — model choice lives in tenant config. |
| Mar 2026 | SharePoint connector downloads file content inline in fetch_documents() | Loaders (PDFLoader, DocxLoader) read raw_doc.content bytes directly. If we stored download_url in extra_metadata and deferred download to loader, every loader would need SharePoint-aware auth. Downloading in the connector keeps loaders auth-agnostic. |
| Mar 2026 | delta_token stored in repo_config.state.delta_token, read by worker at task start | Worker reads state.delta_token from repo_config, passes to get_connector(). On first run: None → full scan. Subsequent runs: token URL → delta scan. Token saved after successful run via save_delta_token(). |
| Mar 2026 | factory.get_connector() now accepts delta_token parameter | Only SharePointConnector uses it — other connectors ignore it. Factory branches on source_type=="sharepoint" to pass it. Clean — no other connector needs changing. |
| Mar 2026 | supports_native_deletes=True on SharePointConnector | Delta API explicitly returns @removed items. Worker Step 4 handles them inline. Worker Step 5 (registry comparison) skipped for SharePoint. |
| Feb 2026 | APScheduler 4.x uses AsyncScheduler + add_schedule() not add_job() | 4.x is a near-complete rewrite of 3.x. Key differences: AsyncScheduler (not AsyncIOScheduler), add_schedule() for recurring triggers, ConflictPolicy.replace (not replace_existing=True), CoalescePolicy.latest (not coalesce=True), async context manager lifecycle. Triggers (IntervalTrigger, CronTrigger) are unchanged. |
| Feb 2026 | SchedulerService is a singleton, JobService injected after init | Avoids circular import at startup. Scheduler module imports nothing from services. JobService is injected in lifespan after all connections are established. |
| Feb 2026 | BUG FIX (Phase 4 pre-flight): job_repo.mark_completed() hardcoded status="completed" | Added partial: bool flag. lifecycle.complete() now passes partial=stats.has_failures. Jobs with per-document errors correctly saved as status="partial" in DB. |
| Feb 2026 | BUG FIX (Phase 4 pre-flight): chunks_deleted stat was 0 for updated documents | ingestion_service step 6 now captures return value of delete_by_source() as chunks_deleted. Returns chunks_deleted in result dict. job_lifecycle.record_document_result() reads result.get("chunks_deleted", 0) for updated docs. |
| Feb 2026 | tenant_id is caller-supplied, not auto-generated | Tenants need stable, meaningful IDs (e.g. "tenant_docassist") that are referenced in every document, every vector chunk, every job record across the platform. Auto-generated UUIDs would be opaque and hard to use operationally. The pattern matches how MongoDB _id works for reference data. |
| Feb 2026 | repo_id is also caller-supplied, same reasoning | Stable IDs like "repo_sp_docassist_main" are referenced by jobs, ingested_documents, and vector chunks. Opaque IDs create operational pain (which repo is failing?). |
| Feb 2026 | Ingest trigger POST /repos/{id}/ingest moved from jobs.py to repos.py | It is semantically a repo action (do something to this repo), not a job action (manage a job record). jobs.py is now purely read-only (GET status, GET history). |
| Feb 2026 | POST /repos/{id}/ingest now validates repo exists before queuing | Phase 1 hardcoded tenant_id — Phase 3 looks up the repo from DB and reads tenant_id from it. Prevents 404s appearing later in the worker. Also rejects is_active=False repos with 409. |
| Feb 2026 | Credentials stored in separate credentials collection, never in source_repositories | Keeps connector_config (safe to log/display) separate from secrets (must never log). credentials_ref field in repo doc links them. Pattern matches SPEC section 5.3. |
| Feb 2026 | index_name resolved at repo creation time, stored in vector_config | Worker just reads vector_config.index_name — no resolution logic in worker. Orchestrator resolves shared_tenant → vidx_tenant_{tenant_id} or dedicated → vidx_repo_{repo_id} at write time. |
| Feb 2026 | MongoDBConnector and SQLConnector completely rewritten | Old stubs were proof-of-concept code incompatible with the DeltaItem/RawDocument contracts. New versions implement full delta strategy, proper source_id, content_hash, error isolation, and correct structured content type routing. |

---

## Appendix B — How to Continue This Project in a New Conversation

1. Share this document (`SPEC.md`) with Claude at the start of the conversation
2. State which phase we are working on next (see Progress Tracker above)
3. Claude will read the relevant sections and continue without repeating context
4. All design decisions, data models, file structure, and code conventions are documented here

**Current state to communicate when resuming:**
- Phase 1 (orchestrator + worker foundation): ✅ Complete — tested Feb 25 2026, all bugs fixed
- Phase 2 (repository pattern): ✅ Complete (merged into Phase 1)
- Phase 2.5 (loader improvements): ✅ Complete — PDF/DOCX two-stage loaders, all connectors rewritten
- Phase 3 (Tenant & Repo Management API): ✅ Complete — Feb 26 2026
- Phase 4 (Delta ingestion): ✅ Complete — all 4 validation runs passed Feb 26 2026
- Phase 5 (Scheduling): ✅ Complete — Feb 26 2026
- Phase 6 (SharePoint Connector): ✅ Complete — Mar 04 2026
- Phase 6b (SQL & MongoDB Connectors): ✅ Complete — Mar 16 2026
- Phase 7 (Enhanced Loaders): ✅ Complete — Mar 15 2026
- Phase 8 (Security Hardening Slimmed): ✅ Complete — Mar 16 2026
- Phase 9 (Batch Embedding & Concurrency): ✅ Complete — Mar 16 2026
- **Ingestion platform: All phases complete ✅**
- Phase R1 (Retrieval Foundation): ✅ Complete — Mar 17 2026
- Phase R2 (Retrieval Core): ✅ Complete — Mar 19 2026
- Phase R3 (REST API): ✅ Complete — Mar 19 2026
- Per-tenant ApiConfig: ✅ Complete — Mar 17 2026
- path_segment_equals metadata source: ✅ Complete — Mar 16 2026
- Security simplified (cross-tenant ownership): ✅ Complete — Mar 16 2026
- **Phase R4 (Filter Architecture + Schema Extensions): ✅ Complete — Mar 20 2026**
- **Phase R5 (Ingestion Quality + Credentials): ✅ Complete — Mar 22 2026**
- **Phase W1 (Credentials Update Endpoint): ✅ Complete — Mar 22 2026**
- **Phase W2/W3 (Notifications — email + webhook): ✅ Complete — Mar 22 2026**
- **XlsxLoader (Excel multi-sheet ingestion): ✅ Complete — Mar 24 2026**
- **MCP Server (retrieval-service): ✅ Complete — Mar 26 2026**

**Post-phase additions (Mar 17 2026) — Per-tenant ApiConfig:**
- `app/schemas/tenant.py` (orchestrator) — ApiConfig model added to tenant schema
- `app/services/tenant_service.py` (orchestrator) — api_config wired through create/patch
- `app/repositories/tenant_repo.py` (orchestrator) — api_config stored in MongoDB
- `app/core/api_config.py` (worker) — NEW: resolve_api_config() shared utility
- `app/core/config.py` (worker) — LLM_MODEL setting added
- `app/providers/embedding/openai_provider.py` (worker) — base_url param added
- `app/worker.py` (worker) — resolve_api_config() wired into embedding provider

**Post-phase additions (Mar 16 2026) — path_segment_equals + security:**
- `app/schemas/tenant.py` (orchestrator) — CustomFieldSchema: match_value, true_value, false_value
- `app/pipeline/metadata_enricher.py` (worker) — path_segment_equals source implemented

**Phase R4 (Mar 20 2026) — Filter Architecture + Schema Extensions:**
- `app/providers/vectorstore/filters.py` (retrieval) — NEW: NormalisedFilter, FieldCondition, FilterConditionWithGeneral
- `app/providers/vectorstore/base.py` (retrieval) — search() accepts NormalisedFilter, _translate_filter() abstract
- `app/providers/vectorstore/mongodb_atlas.py` (retrieval) — _translate_filter() MongoDB $and/$or, vidx_repo_{repo_id}
- `app/services/filter_builder.py` (retrieval) — returns NormalisedFilter, access_filters AND-of-OR semantics
- `app/services/filter_extractor.py` (retrieval) — extractable_fields aware, skip_fields param
- `app/services/retrieval_service.py` (retrieval) — per-repo scoped extraction, extracted dict pattern
- `app/schemas/search.py` (retrieval) — flat filters: dict[str, list[str]], access_filters removed
- `app/schemas/responses.py` (retrieval) — RepoSummary + extractable_fields
- `app/schemas/repo_config.py` (orchestrator) — RetrievalConfig + extractable_fields, IngestionOverrides + metadata_overrides + contextual_enrichment_enabled
- `app/schemas/tenant.py` (orchestrator) — IngestionDefaults + contextual_enrichment_* fields
- `app/repositories/repo_repo.py` (retrieval) — get_distinct_filter_values(repo_ids scoped)
- `app/api/routes.py` (retrieval) — RepoSummary maps extractable_fields

**Phase R5 (Mar 22 2026) — Ingestion Quality Improvements:**
- `app/pipeline/table_converter.py` (worker) — NEW: pipe tables → prose + preceding caption prepend
- `app/pipeline/chunk_contextualiser.py` (worker) — NEW: sliding window LLM context per chunk (Anthropic Contextual Retrieval)
- `app/pipeline/chunker.py` (worker) — breadcrumb prepend to every chunk
- `app/pipeline/metadata_enricher.py` (worker) — repo_metadata_overrides merge with tenant schema
- `app/services/ingestion_service.py` (worker) — Step 3.5 contextual enrichment, table_converter wired
- `app/worker.py` (worker) — contextual_enrichment config resolved + passed to IngestionService

**Phase W1 (Mar 22 2026) — Credentials Update:**
- `app/schemas/repo_config.py` (orchestrator) — UpdateCredentialsRequest, UpdateCredentialsResponse
- `app/repositories/repo_config_repo.py` (orchestrator) — update_credentials()
- `app/services/repo_service.py` (orchestrator) — update_credentials() with source_type validation
- `app/api/repos.py` (orchestrator) — PATCH /repos/{repo_id}/credentials endpoint

**Phase W2/W3 (Mar 22 2026) — Notifications:**
- `app/schemas/notification.py` (orchestrator) — NEW: NotificationConfig, EmailChannel, WebhookChannel
  Auth types: NoneAuth, ApiKeyAuth, BearerTokenAuth, CustomHeadersAuth, HmacAuth, OAuth2Auth
  OAuth2Auth covers Entra ID, Apigee, combined Apigee+Entra via secondary_auth
- `app/schemas/repo_config.py` (orchestrator) — notification_config added to Create/Patch/Response
- `app/services/repo_service.py` (orchestrator) — notification_config persisted
- `app/repositories/repo_config_repo.py` (orchestrator) — notification_config stored
- `app/notifications/` (worker) — NEW package:
  payload_builder.py: summary + full detail payload
  email_notifier.py: SendGrid + dynamic template + plain HTML fallback
  webhook_notifier.py: HTTP POST + retry + exponential backoff
  auth_handler.py: all 6 auth types + OAuth2 token cache
  notification_service.py: trigger check + concurrent channel dispatch
- `app/services/job_lifecycle.py` (worker) — JobStats tracks document lists per result
- `app/worker.py` (worker) — Step 8: notify after job complete + on failure
- `app/core/config.py` (worker) — SENDGRID_API_KEY + template ID settings

**XlsxLoader (Mar 24 2026) — Excel Multi-Sheet Ingestion:**
- `app/loaders/xlsx_loader.py` (worker) — NEW:
  Each sheet → ## SheetName heading (header-aware chunking)
  Row → prose sentence with column headers as context
  Header detection: scans first 3 rows, scores best candidate
  Merged cells: read_only=False to correctly resolve merge ranges
  Hidden sheets skipped, Excel-style column naming (AA/AB beyond Z)
  Links preserved as [label](url)
- `app/loaders/factory.py` (worker) — XlsxLoader registered

**MCP Server (Mar 26 2026):**
- `app/mcp/` (retrieval-service) — NEW package:
  registry.py: TenantMCPRegistry — in-memory dict, sync activate/deactivate
  proxy.py: MCPProxy — single ASGI app at /mcp routes to registry
  server.py: setup_mcp(), activate_tenant(), deactivate_tenant(), shutdown_mcp()
  tools.py: 3 tools — search_documents, query_knowledge_base, list_repos
            Generic filters dict[str, str|list[str]] — works for any tenant
- `app/api/mcp_admin.py` (retrieval-service) — NEW:
  GET  /admin/mcp/tenants
  POST /admin/mcp/{tenant_id}/activate
  POST /admin/mcp/{tenant_id}/deactivate
- `app/repositories/tenant_repo.py` (retrieval-service) — list_active() added
- `main.py` (retrieval-service) — setup_mcp() in lifespan

**Key MCP architectural decisions:**
- stateless_http=True — tool calls are independent, no session state needed
- Registry+proxy pattern — one static /mcp mount, dynamic tenant routing
- Tenant deactivation instant (no restart) — remove from registry dict
- Generic filters — not hardcoded to DocAssist fields
- In-memory registry — Redis not needed for bounded PwC tenant count
- Endpoint: /mcp/{tenant_id}/mcp (fastmcp adds /mcp suffix to http_app)
- MCP Inspector test: npx @modelcontextprotocol/inspector
  Connect to: http://localhost:8001/mcp/{tenant_id}/mcp

**Key architectural decisions (Mar 20-22 2026):**
- SearchFilters: flat `filters: dict[str, list[str]]` — caller passes access control lists
- FilterExtractor only runs on `extractable_fields` (content dims) — never access_group/source_id
- Per-repo Atlas index: `vidx_repo_{repo_id}` — each repo has exactly its filterable_fields
- access_group via path convention: docassist-test/domain/app/access_group/file
- Path indexes for DocAssist: domain=1, application=2, access_group=3, is_general=1
- ChunkContextualiser: sliding window ±2 chunks, concurrency-safe snapshots
- TableConverter: converts ≤4 col tables to prose, prepends preceding paragraph
- `app/core/security.py` (orchestrator) — simplified to verify_repo_ownership() only
- `app/api/repos.py` (orchestrator) — tenant_id query param for ownership
- `app/api/schedules.py` (orchestrator) — tenant_id query param for ownership
- `app/api/jobs.py` (orchestrator) — tenant_id query param for ownership

**Files changed in Phase 8 (Mar 16 2026):**
- `app/core/security.py` (orchestrator) — NEW: get_requesting_tenant(), verify_repo_ownership(), verify_tenant_access()
- `app/api/dependencies.py` (orchestrator) — RequestingTenantDep added
- `app/api/repos.py` (orchestrator) — ownership check on get/patch/delete/ingest
- `app/api/schedules.py` (orchestrator) — ownership check on all 4 schedule endpoints
- `app/api/jobs.py` (orchestrator) — tenant access check on list_jobs_for_tenant
- `app/core/config.py` (orchestrator) — REDIS_PASSWORD + TENANT_HEADER_NAME added
- `app/queue/producer.py` (orchestrator) — REDIS_PASSWORD wired into RedisSettings
- `app/core/config.py` (worker) — REDIS_PASSWORD added
- `app/worker.py` (worker) — REDIS_PASSWORD wired into RedisSettings
- **Credential scrubbing: already in place in both logging.py files — no changes needed**

**Files changed in Phase 9 (Mar 16 2026):**
- `app/providers/embedding/openai_provider.py` (worker) — batch_size + concurrency params, concurrent batch calls via TaskGroup + Semaphore
- `app/services/ingestion_service.py` (worker) — deduplicator property added
- `app/worker.py` (worker) — reads document_concurrency/embedding_batch_size/embedding_concurrency from tenant config; document loop replaced with TaskGroup + Semaphore; startup provider uses settings.EMBEDDING_MODEL

**Files changed in Phase 6b (Mar 16 2026):**
- `app/connectors/sql_connector.py` (worker) — SQLConnector with asyncpg/aiomysql, batch pagination, delta_column support
- `app/connectors/mongodb_connector.py` (worker) — MongoDBConnector with pymongo async, filter_query, delta_field support
- `app/connectors/factory.py` (worker) — added last_run_at param, injected into SQL/MongoDB connectors
- `app/loaders/sql_loader.py` (worker) — SQL row → Markdown table
- `app/loaders/mongodb_loader.py` (worker) — MongoDB document → nested Markdown sections
- `app/worker.py` (worker) — reads state.last_successful_run_at, saves after sql/mongodb run
- `requirements.txt` (worker) — sqlalchemy[asyncio], asyncpg, aiomysql (already present)
- **Orchestrator: NO changes needed** — repo creation API already supports sql/mongodb source_types

**Files changed in Phase 7 (Mar 15 2026):**
- `app/loaders/pptx_loader.py` (worker) — NEW: slide-aware PPTX loader, tables, speaker notes, Vision image descriptions
- `app/loaders/factory.py` (worker) — registered PptxLoader for both PPTX MIME types
- `app/pipeline/metadata_enricher.py` (worker) — implemented _extract_custom_fields() (path_segment, file_name, static)
- `app/worker.py` (worker) — per-tenant embedding model selection from ingestion_defaults
- `app/core/config.py` (worker) — added EMBEDDING_MODEL setting with default
- `requirements.txt` (worker) — added python-pptx>=1.0.0

**Files changed in Phase 6 (Mar 04 2026):**
- `app/connectors/sharepoint.py` (worker) — full SharePoint connector, OAuth2 client credentials, full + delta scan, pagination, filters
- `app/connectors/factory.py` (worker) — updated import, added delta_token param, SharePoint branch
- `app/worker.py` (worker) — reads state.delta_token from repo_config, passes to get_connector()
- **Orchestrator: NO changes needed** — delta_token save already wired in Phase 1

**Files changed in Phase 5 (Feb 26 2026):**
- `app/services/scheduler_service.py` — APScheduler 4.x AsyncScheduler, register/unregister/pause/resume, singleton
- `app/api/schedules.py` — GET/PUT/pause/resume schedule endpoints
- `app/api/dependencies.py` — added SchedulerServiceDep
- `main.py` — APScheduler start/stop in lifespan, startup loads all scheduled repos

**Files changed in Phase 4 pre-flight (Feb 26 2026):**
- `app/repositories/job_repo.py` (worker) — mark_completed() now accepts partial= flag, writes correct status
- `app/services/job_lifecycle.py` (worker) — complete() passes partial=stats.has_failures to repo
- `app/services/ingestion_service.py` (worker) — step 6 captures chunks_deleted, returns in result dict

**Files changed in Phase 3 (Feb 26 2026):**
- `app/schemas/tenant.py` — CreateTenantRequest, PatchTenantRequest, TenantResponse
- `app/schemas/schedule.py` — ScheduleConfig with mode validation
- `app/schemas/repo_config.py` — discriminated union ConnectorConfig, CreateRepoRequest, RepoResponse
- `app/repositories/tenant_repo.py` — create, get_by_id, update, exists
- `app/repositories/repo_config_repo.py` (orchestrator) — full CRUD + credentials split + index resolution
- `app/repositories/job_repo.py` — added list_for_tenant()
- `app/api/dependencies.py` — added TenantRepoDep, RepoConfigRepoDep, JobRepoDep
- `app/api/tenants.py` — POST/GET/PATCH /tenants
- `app/api/repos.py` — full repo CRUD + ingest trigger
- `app/api/jobs.py` — cleaned up, added GET /tenants/{id}/jobs
- `app/core/exceptions.py` — added RepoAlreadyExistsError
- `main.py` — registered tenants + repos routers
- **Worker: NO changes needed** — worker reads exact same fields it always did

**Files changed in Phase 2.5 (Feb 25 2026):**
- `app/loaders/pdf_loader.py` — replaced Docling with pymupdf4llm + Vision enrichment
- `app/loaders/docx_loader.py` — added complexity detection + Vision enrichment
- `app/loaders/factory.py` — added llm= parameter
- `app/services/ingestion_service.py` — added loader_llm= parameter
- `app/worker.py` — added ChatOpenAI loader LLM init (step 4)
- `app/connectors/base.py` — added supports_native_deletes, raw_data, structured_source_type
- `app/connectors/local_connector.py` — renamed from local.py, corrected content_type
- `app/connectors/sharepoint_connector.py` — renamed from sharepoint.py, added supports_native_deletes=True
- `app/connectors/mongodb_connector.py` — complete rewrite with proper DeltaItem/RawDocument
- `app/connectors/sql_connector.py` — complete rewrite with LIMIT/OFFSET pagination
- `app/connectors/factory.py` — registered mongodb + sql connectors

*No context is lost between conversations as long as this document is up to date.*

---

## 20. Retrieval Service — `docassist-retrieval`

### 20.1 Overview

Standalone FastAPI microservice. Dual interface: REST API + MCP Server.
Same MongoDB Atlas cluster as ingestion platform — reads from `vector_store` collection.
Shares `api_config.py` and `encryption.py` with ingestion worker (copied files).

```
Client (Copilot Studio / Claude / Agent)
  → Ocelot Gateway
    → docassist-retrieval (FastAPI)
        ├── REST  POST /api/v1/{tenant_id}/search   → chunks only
        ├── REST  POST /api/v1/{tenant_id}/query    → answer + sources
        └── MCP   GET  /mcp/{tenant_id}/sse         → SSE tools for agents
              ↓
        RetrievalService
          → read tenant from MongoDB (api_config, ingestion_defaults)
          → read active repos from MongoDB (retrieval_config per repo)
          → decrypt genai_api_key via encryption.py
          → embed question with same model used at ingestion
          → build pre_filter per repo from retrieval_config
          → $vectorSearch on vidx_tenant_{tenant_id}
          → merge + rank results
              ↓ (for /query only)
        RAGService
          → build prompt (question + chunks)
          → LLM answer generation (tenant llm_model from api_config)
          → return answer + sources
```

### 20.2 Folder Structure

```
docassist-retrieval/
├── main.py
├── requirements.txt
├── .env.example
└── app/
    ├── core/
    │   ├── config.py          ← MONGO_URI, OPENAI_API_KEY, LLM_MODEL, SECRET_ENCRYPTION_KEY
    │   ├── logging.py         ← same structlog pattern + SafeStreamHandler
    │   ├── exceptions.py
    │   ├── encryption.py      ← COPY from worker (same file)
    │   └── api_config.py      ← COPY from worker (same file)
    ├── db/
    │   └── mongo.py           ← same pattern as worker
    ├── repositories/
    │   ├── tenant_repo.py     ← read tenant (api_config, ingestion_defaults, metadata_schema)
    │   └── repo_repo.py       ← read repos (retrieval_config, vector_config)
    ├── providers/
    │   ├── embedding.py       ← OpenAIEmbeddingProvider (simplified from worker)
    │   └── vectorstore.py     ← similarity_search() wrapper — read only
    ├── services/
    │   ├── filter_builder.py  ← build $vectorSearch pre_filter per repo
    │   ├── retrieval_service.py ← embed → search → merge
    │   └── rag_service.py     ← retrieve → prompt → LLM → answer
    ├── api/
    │   ├── dependencies.py
    │   ├── search.py          ← POST /api/v1/{tenant_id}/search
    │   ├── query.py           ← POST /api/v1/{tenant_id}/query
    │   └── health.py
    └── mcp/
        ├── server.py          ← FastAPI SSE endpoint /mcp/{tenant_id}/sse
        └── tools.py           ← search_documents, query_knowledge_base, list_repos
```

### 20.3 REST API

#### `POST /api/v1/{tenant_id}/search`
Returns relevant chunks — no LLM involved. Fast.

```json
Request:
{
  "question": "How do I configure AppA gateway?",
  "filters": {
    "application": "AppA",
    "include_general": true,
    "domain": null,
    "repo_ids": null
  },
  "top_k": 5
}

Response:
{
  "chunks": [
    {
      "text": "To configure AppA...",
      "score": 0.91,
      "file_name": "AppA_Config.pdf",
      "full_path": "domain1/AppA/AppA_Config.pdf",
      "source_url": "https://...",
      "domain": "domain1",
      "application": "AppA",
      "is_general": "false",
      "chunk_index": 3,
      "repo_id": "docassist_repo_dev"
    }
  ],
  "total": 5
}
```

#### `POST /api/v1/{tenant_id}/query`
Full RAG — embed → search → LLM answer.

```json
Request:
{
  "question": "How do I configure AppA gateway?",
  "filters": {
    "application": "AppA",
    "include_general": true
  },
  "top_k": 5,
  "stream": false
}

Response:
{
  "answer": "To configure AppA gateway, first navigate to...",
  "sources": [
    {
      "file_name": "AppA_Config.pdf",
      "source_url": "https://...",
      "application": "AppA",
      "domain": "domain1",
      "score": 0.91
    }
  ],
  "query_id": "uuid",
  "tokens_used": { "prompt": 1200, "completion": 350 }
}
```

### 20.4 Filter Builder Logic

Each repo has a `retrieval_config` defining which fields are filterable.
The filter builder applies only supported fields per repo — enabling
safe cross-repo search.

```python
# filters from request: { application: "AppA", include_general: True }
# repo.retrieval_config: { filterable_fields: ["domain","application","is_general"],
#                          general_flag_field: "is_general",
#                          general_flag_value: "true" }

def build_pre_filter(tenant_id, repo, filters):
    base = {
        "tenant_id": tenant_id,
        "repo_id": repo["_id"]
    }
    filterable = repo["retrieval_config"]["filterable_fields"]

    # Domain filter — only if repo supports it
    if filters.domain and "domain" in filterable:
        base["domain"] = filters.domain

    # Application + general OR logic
    if filters.application and "application" in filterable:
        flag_field = repo["retrieval_config"].get("general_flag_field")
        flag_value = repo["retrieval_config"].get("general_flag_value", "true")

        if filters.include_general and flag_field and flag_field in filterable:
            base["$or"] = [
                { "application": filters.application },
                { flag_field: flag_value }
            ]
        else:
            base["application"] = filters.application

    return base

# SharePoint repo → { tenant_id, repo_id, $or: [{application:"AppA"},{is_general:"true"}] }
# SQL repo        → { tenant_id, repo_id }  ← application not in filterable_fields, skipped
```

### 20.5 MCP Server

```
GET /mcp/{tenant_id}/sse    ← SSE connection, per-tenant endpoint

Tools exposed:
  search_documents
    input:  question, application (optional), include_general (optional), top_k
    output: list of chunks with source metadata

  query_knowledge_base
    input:  question, application (optional), include_general (optional), top_k
    output: LLM-generated answer + sources

  list_repos
    input:  (none)
    output: list of active repos for this tenant with their filterable_fields
```

### 20.6 Phase Plan

| Phase | Deliverable | Status | Key files |
|---|---|---|---|
| **R1** | Foundation — config, logging, MongoDB, health | ✅ Done — Mar 17 2026 | `main.py`, `core/`, `db/`, `health.py` |
| **R2** | Retrieval core — embed + $vectorSearch + filter builder | ✅ Done — Mar 19 2026 | `providers/`, `repositories/`, `services/filter_builder.py`, `services/retrieval_service.py` |
| **R3** | REST API — `/search`, `/query`, `/repos` | ✅ Done — Mar 19 2026 | `schemas/`, `services/llm_service.py`, `api/routes.py` |
| **R4 (done)** | Filter Architecture — NormalisedFilter, access_group, extractable_fields, per-repo index | ✅ Mar 20 2026 | `filters.py`, `filter_builder.py`, `filter_extractor.py`, `search.py` |
| **R5 (done)** | Ingestion Quality — TableConverter, ChunkContextualiser (sliding window), metadata_overrides | ✅ Mar 22 2026 | `table_converter.py`, `chunk_contextualiser.py`, `metadata_enricher.py` |
| **W1 (done)** | Credentials Update Endpoint | ✅ Mar 22 2026 | `repos.py`, `repo_service.py`, `repo_config_repo.py` |
| **W2/W3 (done)** | Notifications — SendGrid email + webhook, all auth types | ✅ Mar 22 2026 | `notification.py`, `notifications/` |
| **XLSX (done)** | XlsxLoader — multi-sheet Excel ingestion | ✅ Mar 24 2026 | `loaders/xlsx_loader.py` |
| **MCP (done)** | MCP Server — registry+proxy, 3 tools, dynamic activate/deactivate | ✅ Mar 26 2026 | `mcp/` |
| **R6a (done)** | Document Identity Prefix — tenant-driven, zero hardcoded fields | ✅ Mar 28 2026 | `pipeline/document_prefix_builder.py` |
| **R6b (done)** | Contextual Enrichment — semantic 2-sentence LLM context per chunk | ✅ Mar 28 2026 | `pipeline/chunk_contextualiser.py` |
| **R6d (done)** | Hybrid Search — $vectorSearch + $search + $rankFusion | ✅ Mar 28 2026 | `providers/vectorstore/mongodb_atlas.py` |
| **R6e (done)** | Reranker — tenant LLM cross-encoder, MAX_RERANK_CHARS=400 | ✅ Mar 30 2026 | `services/reranker.py` |
| **C1 (done)** | OneDrive Connector — Graph API delta, native deletes | ✅ Mar 30 2026 | `connectors/onedrive.py` |
| **C2 (done)** | Azure Blob Connector — last_modified delta, DefaultAzureCredential | ✅ Mar 30 2026 | `connectors/azure_blob.py` |
| **C3** | Teams Files — skip (SharePoint under the hood, use site_url instead) | ⏭️ Skipped | N/A |
| **C4** | ServiceNow Connector — Table API, kb_knowledge first | 🔲 Backlog | `connectors/servicenow.py` |
| **v10** | must/should filters + repo-level schema + computed field + metadata_overrides removed | ✅ Apr 20 2026 | retrieval + orchestrator + worker |
| **ADM** | Admin User + Force Ingestion + Entra/OpenAM JWK auth | 🔲 Next | `orchestrator/api/`, `orchestrator/core/` |
| **UI** | Angular Admin UI — manage tenants, repos, jobs, users | 🔲 Next | `admin-ui/` |

### 20.7 Phase R1 — Foundation Details

**Service name:** `retrieval-service/`  
**Dev port:** `8001` (orchestrator on 8000, no conflict)  
**K8s port:** container-internal — Ocelot/ingress handles external routing  
**Python:** 3.13 (same as ingestion worker)

**Files created:**

| File | Purpose |
|---|---|
| `main.py` | FastAPI app + lifespan: logging → MongoDB → routes |
| `app/core/config.py` | Settings: MONGO_URI, OPENAI_API_KEY, LLM_MODEL, EMBEDDING_MODEL, OPENAI_API_VERSION, SECRET_ENCRYPTION_KEY, KEY_VAULT_URL |
| `app/core/logging.py` | SafeStreamHandler + noisy logger suppression (same pattern as worker) |
| `app/core/exceptions.py` | TenantNotFoundError, TenantInactiveError, NoActiveReposError, EmbeddingError, VectorSearchError, LLMError |
| `app/core/encryption.py` | **COPIED** from worker — decrypt_value(), decrypt_credentials() |
| `app/core/api_config.py` | **COPIED** from worker — resolve_api_config() |
| `app/db/mongo.py` | AsyncMongoClient — connect/close/get_database() |
| `app/api/health.py` | GET /health/live + GET /health/ready |
| `requirements.txt` | All dependencies |
| `.env.example` | Template with all required env vars |

**Key settings added vs worker:**
- `OPENAI_API_VERSION` — Azure OpenAI API version (default: `2024-08-01-preview`)
- `PORT` — local dev port (default: 8001)
- `genai_api_key` added to `_SENSITIVE_KEYS` in logging

**MCP transport decision:**
SSE transport deprecated effective April 1 2026. Using Streamable HTTP (`fastmcp>=3.1.1`).
Mount pattern: one FastMCP instance per active tenant at `/mcp/{tenant_id}`.
Tools are pre-scoped to tenant — no `tenant_id` parameter in tool calls.

**To run locally:**
```bash
cd retrieval-service
pip install -r requirements.txt
cp .env.example .env   # fill in values
uvicorn main:app --reload --port 8001
# GET http://localhost:8001/health/ready → { "status": "ready", "mongodb": "ok" }
```

### 20.7 Shared Contracts from Ingestion Platform

| Contract | Value |
|---|---|
| Vector collection | `vector_store` |
| Text key | `text` |
| Embedding key | `embedding` |
| Index (shared_tenant) | `vidx_tenant_{tenant_id}` |
| Index (dedicated) | `vidx_repo_{repo_id}` |
| Embedding model | `tenant.ingestion_defaults.embedding_model` |
| LLM model | `tenant.api_config.llm_model` (default: `gpt-4.1-mini`) |
| API key | `tenant.api_config.genai_api_key` → decrypt via `encryption.py` |
| Base URL | `tenant.api_config.genai_base_url` |
| Filter fields | `repo.retrieval_config.filterable_fields` |
| General flag field | `repo.retrieval_config.general_flag_field` |
| General flag value | `repo.retrieval_config.general_flag_value` |


### 20.8 Phase R2 — Retrieval Core Details (Mar 19 2026)

**Files created:**

| File | Purpose |
|---|---|
| `app/repositories/base.py` | `BaseRepository(ABC)` — same pattern as orchestrator |
| `app/repositories/tenant_repo.py` | `TenantRepository` — reads tenant + api_config |
| `app/repositories/repo_repo.py` | `RepoRepository` — reads repos + retrieval_config + list_for_tenant() |
| `app/schemas/search.py` | `SearchFilters` Pydantic model — generic `metadata: dict[str, str]` + `include_general` |
| `app/providers/embedding/base.py` | `BaseEmbeddingProvider(ABC)` — embed_query(), model_name, dimensions |
| `app/providers/embedding/openai_provider.py` | `OpenAIEmbeddingProvider` — native `aembed_query()`, no thread pool |
| `app/providers/embedding/factory.py` | `EmbeddingProviderFactory` — only place that imports concrete provider |
| `app/providers/vectorstore/base.py` | `BaseVectorStoreProvider(ABC)` + `SearchResult` dataclass with `to_dict()` |
| `app/providers/vectorstore/mongodb_atlas.py` | `MongoDBAtlasVectorStoreProvider` — uses `asyncio.to_thread(_similarity_search_with_score)` |
| `app/providers/vectorstore/factory.py` | `build_vectorstore_provider()` — reads `VECTOR_STORE_PROVIDER` from settings |
| `app/services/filter_builder.py` | `FilterBuilder` — generic iteration over `filters.metadata` keys vs `filterable_fields` |
| `app/services/retrieval_service.py` | `RetrievalService.search()` — embed once → search repos concurrently → merge → rank |
| `app/api/dependencies.py` | FastAPI DI — singletons + per-request wiring + `RetrievalServiceDep` |

**Key design decisions:**

| Decision | Rationale |
|---|---|
| `SearchFilters.metadata: dict[str, str]` (generic) | Field names like `application`, `domain` are tenant-specific — hardcoding them in a generic schema is wrong. Each tenant/repo defines its own filterable fields via `retrieval_config`. |
| `EmbeddingProviderFactory` injected into `RetrievalService` | Service never imports concrete `OpenAIEmbeddingProvider` — factory is the only place. Swapping providers = change one line in factory. |
| `_similarity_search_with_score(query_vector, pre_filter)` via `asyncio.to_thread` | Only internal LangChain method that accepts pre-computed vector AND returns scores. Public async methods either re-embed (`asimilarity_search_with_score`) or drop scores (`asimilarity_search_by_vector`). |
| `repo_ids` as direct param on `search()`, not in `SearchFilters` | `SearchFilters` controls what to filter INSIDE a repo. `repo_ids` controls WHICH repos to search — different concern. |
| `per_repo_k = top_k * 2` | Fetch 2x candidates per repo so final merge+rank has enough to produce a good top_k across all repos. |
| Sync `MongoClient` in vectorstore provider | LangChain's `MongoDBAtlasVectorSearch.__init__` requires sync pymongo `Collection` — LangChain constraint. All other DB access uses `AsyncMongoClient`. |

**Atlas Vector Search Index requirement:**

`$vectorSearch` pre_filter only works on fields explicitly declared as filter fields in the Atlas index.
`tenant_id` and `repo_id` MUST be filter fields — without them pre_filter is silently ignored (all tenants searched — security hole).

```json
{
  "fields": [
    { "type": "vector", "path": "embedding", "numDimensions": 1536, "similarity": "cosine" },
    { "type": "filter", "path": "tenant_id" },
    { "type": "filter", "path": "repo_id" },
    { "type": "filter", "path": "domain" },
    { "type": "filter", "path": "application" },
    { "type": "filter", "path": "is_general" }
  ]
}
```

Index name: `vidx_tenant_{tenant_id}` (shared_tenant mode, default).
`numDimensions` must match `embedding_model`: text-embedding-3-small → 1536, text-embedding-3-large → 3072.

---

### 20.9 Phase R3 — REST API Details (Mar 19 2026)

**Files created:**

| File | Purpose |
|---|---|
| `app/schemas/requests.py` | `SearchRequest`, `QueryRequest` — Pydantic request bodies with validation |
| `app/schemas/responses.py` | `ChunkResponse`, `SearchResponse`, `QueryResponse`, `ReposResponse`, `RepoSummary` |
| `app/services/llm_service.py` | `LLMService.generate_answer()` — RAG answer using tenant `llm_model` + `api_key`, native `ainvoke()` |
| `app/api/routes.py` | 3 endpoints with domain exception → HTTP status mapping |

**REST API — Updated Contract:**

`POST /api/v1/{tenant_id}/search`
```json
Request:
{
  "question": "What is the leave policy?",
  "filters": {
    "metadata": { "application": "AppA" },
    "include_general": true
  },
  "top_k": 5,
  "repo_ids": null
}

Response:
{
  "question": "What is the leave policy?",
  "chunks": [
    {
      "text": "...",
      "score": 0.91,
      "repo_id": "abc123",
      "file_name": "LeavePolicy.pdf",
      "source_url": "https://...",
      "metadata": { "application": "AppA", "domain": "HR", "is_general": "false" }
    }
  ],
  "total": 5,
  "repos_searched": 2
}
```

`POST /api/v1/{tenant_id}/query`
```json
Request:
{
  "question": "How many leave days do contractors get?",
  "filters": { "metadata": {}, "include_general": true },
  "top_k": 5
}

Response:
{
  "question": "How many leave days do contractors get?",
  "answer": "Contractors receive 15 days of annual leave...",
  "chunks": [ ... ],
  "total_chunks": 5
}
```

`GET /api/v1/{tenant_id}/repos`
```json
Response:
{
  "tenant_id": "docassist_dev",
  "repos": [
    {
      "repo_id": "abc123",
      "name": "SharePoint HR",
      "source_type": "sharepoint",
      "filterable_fields": ["domain", "application", "is_general"]
    }
  ],
  "total": 1
}
```

**Error mapping:**

| Domain Exception | HTTP Status |
|---|---|
| `TenantNotFoundError` | 404 Not Found |
| `TenantInactiveError` | 403 Forbidden |
| `NoActiveReposError` | 404 Not Found |
| `EmbeddingError` | 502 Bad Gateway |
| `VectorSearchError` | 502 Bad Gateway |
| `LLMError` | 502 Bad Gateway |

### 20.10 Key Design Decisions

| Decision | Rationale |
|---|---|
| tenant_id in URL path (`/{tenant_id}/search`) | Ocelot can enforce tenant routing at gateway level without touching request body |
| repo_ids optional in request body | shared_tenant index covers all repos in one search — explicit scoping optional |
| retrieval_config per repo, not global | Different repos have different metadata schemas — global filter would silently miss documents on repos that don't have those fields |
| filter_builder reads retrieval_config per repo | Only applies filters the repo supports — safe cross-repo search |
| Same encryption.py and api_config.py | Single source of truth — copy to retrieval service, no duplication of logic |
| Embedding model from ingestion_defaults | Must use same model as ingestion — different models = incompatible vector spaces = garbage results |
| MCP per-tenant SSE endpoint | Each tenant gets dedicated endpoint — clean separation, Ocelot can route per-tenant |


---

## 22. Pending Backlog

### 22.1 Admin User + Force Ingestion (ADM)

**Priority:** High — needed before production rollout

**Admin User feature:**
```
Problem:
  Currently any API call with a valid tenant_id can trigger ingestion.
  No role-based access control within the platform itself.
  PwC needs an admin role to manage tenants, repos, and jobs.

Solution:
  admin_key per tenant stored in tenants collection (Fernet encrypted)
  Admin endpoints protected by X-Admin-Key header
  Regular endpoints remain accessible via Ocelot/Entra auth

New endpoints (orchestrator):
  POST /admin/tenants                    ← create tenant (admin only)
  DELETE /admin/tenants/{tenant_id}      ← deactivate tenant
  GET /admin/tenants                     ← list all tenants
  GET /admin/tenants/{tenant_id}/jobs    ← job history for tenant
  POST /admin/repos/{repo_id}/force-ingest ← force re-ingestion
  DELETE /admin/repos/{repo_id}/chunks   ← wipe all chunks for repo
```

**Force Ingestion feature:**
```
Problem:
  When pipeline changes (R6a, R6b prompt, chunker), existing chunks
  have old embeddings. Currently must manually update content_hash
  in MongoDB to force re-ingestion.

Solution:
  POST /admin/repos/{repo_id}/force-ingest
  
  What it does:
    1. Resets content_hash to "force_reingest" for all documents in repo
    2. Triggers immediate ingestion job via ARQ
    3. Returns job_id for tracking
  
  Options:
    force_ingest_all: bool   ← reset all docs in repo (default True)
    source_id: str | None    ← reset single document only
    reason: str              ← logged for audit trail

  MongoDB update:
    db.ingested_documents.updateMany(
      { tenant_id, repo_id },
      { $set: { content_hash: "force_reingest", force_reason: reason } }
    )
```

**Files to change:**
```
ingestion-orchestrator:
  app/api/admin.py          ← NEW — admin endpoints
  app/core/admin_auth.py    ← NEW — X-Admin-Key validation
  app/schemas/admin.py      ← NEW — ForceIngestRequest, AdminTenantResponse
  app/services/admin_service.py ← NEW — force ingest logic
  app/repositories/tenant_repo.py ← add admin_key support
  main.py                   ← register /admin router

ingestion-worker:
  No changes — force_reingest hash triggers normal pipeline
```

---

### 22.2 Angular Admin UI (UI)

**Priority:** High — needed for PwC demo and production ops

**What it manages:**
```
Tenant management:
  - Create / view / deactivate tenants
  - Set ingestion_defaults (chunk_size, embedding_model, enrichment)
  - Set api_config (genai_api_key, base_url, llm_model)

Repo management:
  - Create / edit / delete repos
  - Configure connector (source_type, connector_config)
  - Set credentials (encrypted at rest)
  - Set retrieval_config (filterable_fields, extractable_fields, hybrid)
  - Set metadata_schema (custom fields from path segments)
  - Set notification_config (email/webhook)

Job monitoring:
  - List recent jobs per repo
  - Job status (running/success/failed)
  - Documents processed/failed/skipped counts
  - Error details per document

Force ingestion:
  - One-click "Force Re-Ingest" button per repo
  - Shows confirmation dialog with reason input
  - Displays resulting job_id

Chunk explorer:
  - Search chunks by repo (for debugging)
  - Shows chunk text, score, metadata
  - Useful for verifying R6a/R6b output

Live ingestion status:
  - WebSocket or polling for running job progress
  - Documents ingested / total count
  - Current file being processed
```

**Tech stack:**
```
Angular 18+ (latest)
Angular Material or PrimeNG for components
Angular Signals for state management
OnPush change detection
Lazy-loaded feature modules:
  /tenants    ← tenant management
  /repos      ← repo management
  /jobs       ← job monitoring
  /chunks     ← chunk explorer (debug tool)

Auth:
  Azure AD / MS Entra SSO (matches PwC environment)
  OR simple X-Admin-Key header for dev

API:
  Calls ingestion-orchestrator REST API
  Calls retrieval-service /search for chunk explorer
```

**Effort estimate:** ~5-7 days

---

### 22.3 ServiceNow Connector (C4 — Backlog)

**Priority:** Medium — add when PwC ServiceNow integration requested

**Design:**
```
source_type: "servicenow"

connector_config:
  instance_url:   "https://pwc.service-now.com"
  table:          "kb_knowledge"           ← or incident, problem, change_request
  text_fields:    ["short_description", "text", "category"]
  title_field:    "short_description"
  delta_field:    "sys_updated_on"         ← standard on all ServiceNow tables
  filter:         "workflow_state=published"  ← optional OData filter

credentials:
  username:       "service_account@pwc.com"
  password:       "encrypted:..."
  OR
  client_id + client_secret (OAuth2)

Delta detection:
  GET /api/now/table/{table}?sysparm_query=sys_updated_on>{last_run_at}
  Standard across ALL tables ✅

Pagination:
  sysparm_limit + sysparm_offset
  Standard across ALL tables ✅

Deletion detection:
  No native delete API
  supports_native_deletes = False
  Worker Step 5 registry diff ✅

Effort: ~2-3 days
```

---

### 22.4 Angular DocAssist UI (Backlog)

**Priority:** Medium — needed for end-user facing product

**What it is:**
```
End-user chat interface for DocAssist (RAG chatbot)
Different from Admin UI — this is for regular users

Features:
  - Chat interface (like ChatGPT)
  - File/document scope selector (filter by application/domain)
  - Source citation with chunk preview
  - Conversation history
  - Feedback (thumbs up/down per answer)

Tech stack:
  Angular 18+
  Same as Admin UI but different feature modules
  
Effort: ~3-4 days
```

---

### 22.5 AKS Deployment (Backlog)

**Priority:** High — needed for production

**What's needed:**
```
Already have:
  docker-compose.yml
  Dockerfile per service
  k8s-deploy.yml (partial)
  k8s-secret.yml
  k8s-service.yml

Still needed:
  Helm charts for all 3 microservices
  AKS ingress (Ocelot or NGINX)
  Azure Key Vault integration (replace Fernet for prod)
  CI/CD pipeline (Azure DevOps)
  Horizontal pod autoscaler config
  
Effort: ~3-4 days
```


---

## 23. Three-Tier Vector Collection Routing

### 23.1 Problem

```
One collection (vector_store) for all tenants and repos:
  Works fine at small scale (dev/PoC)
  Degrades at production scale:
    - HNSW graph spans ALL vectors regardless of pre-filter
    - One tenant's heavy write load affects all query latency
    - Atlas index RAM grows unbounded
    - Hard to delete/isolate a tenant's data
```

### 23.2 Solution — Three Tiers

| Tier | index_mode | Collection | Index | Use When |
|---|---|---|---|---|
| **Shared** | `shared_tenant` | `vector_store` | `vidx_tenant_{tenant_id}` | Dev / small tenants / < 100k total vectors |
| **Dedicated Tenant** | `dedicated_tenant` | `vector_store_{tenant_id}` | `vidx_tenant_{tenant_id}` | Tenant has multiple repos or > 100k vectors |
| **Dedicated Repo** | `dedicated_repo` | `vector_store_{repo_id}` | `vidx_repo_{repo_id}` | Single repo expected to grow > 500k vectors |

### 23.3 How It Works

```
At repo creation (orchestrator):
  repo_service.py calls resolve_collection_name()
  collection_name stored in vector_config.collection_name

At ingestion (worker):
  worker.py reads vector_config.collection_name
  passes to MongoDBAtlasVectorStore(collection_name=...)
  all writes go to correct collection

At retrieval:
  retrieval_service.py reads vector_config.collection_name
  passes to mongodb_atlas.search(collection_name=...)
  HNSW graph traversal bounded to that collection only
```

### 23.4 Files Changed

```
ingestion-orchestrator:
  schemas/repo_config.py
    VectorConfig.index_mode: "shared_tenant" | "dedicated_tenant" | "dedicated_repo"
    VectorConfig.collection_name: str | None  ← resolved + stored at creation

  repositories/repo_config_repo.py
    resolve_collection_name()  ← NEW
    resolve_index_name()       ← updated for three modes
    resolve_search_index_name() ← updated for three modes

  services/repo_service.py
    vector_config_dict["collection_name"] = resolve_collection_name()

ingestion-worker:
  providers/vectorstore/mongodb_atlas.py
    MongoDBAtlasVectorStore(collection_name=...)  ← dynamic collection
    resolve_collection_name() helper

  worker.py
    collection_name = vector_config.get("collection_name", "vector_store")
    passed to MongoDBAtlasVectorStore

retrieval-service:
  providers/vectorstore/mongodb_atlas.py
    _get_collection(collection_name)  ← returns correct sync collection
    _execute_search(collection=...)
    _execute_hybrid_search(collection=...)
    search(collection_name=...)

  services/retrieval_service.py
    collection_name = vector_cfg.get("collection_name", "vector_store")
    passed to vector_store.search()
```

### 23.5 Promotion Path

```
When a repo/tenant grows beyond threshold:

1. Update index_mode in repo config:
   db.repos.updateOne(
     { _id: ObjectId("<repo_id>") },
     { $set: {
       "vector_config.index_mode": "dedicated_repo",
       "vector_config.collection_name": "vector_store_<repo_id>"
     }}
   )

2. Create new Atlas vector index on new collection:
   vidx_repo_{repo_id} on vector_store_{repo_id}

3. Migrate existing chunks:
   db.vector_store.aggregate([
     { $match: { repo_id: "<repo_id>" } },
     { $out: "vector_store_<repo_id>" }
   ])

4. Delete migrated chunks from shared collection:
   db.vector_store.deleteMany({ repo_id: "<repo_id>" })

5. No code deployment needed — config change only
```

### 23.6 Atlas Index Requirements Per Tier

```
shared_tenant:
  One vector index on "vector_store": vidx_tenant_{tenant_id}
  One search index on "vector_store": sidx_tenant_{tenant_id}

dedicated_tenant:
  One vector index on "vector_store_{tenant_id}": vidx_tenant_{tenant_id}
  One search index on "vector_store_{tenant_id}": sidx_tenant_{tenant_id}

dedicated_repo:
  One vector index on "vector_store_{repo_id}": vidx_repo_{repo_id}
  One search index on "vector_store_{repo_id}": sidx_repo_{repo_id}

Note: Atlas recommends dedicated Search Nodes for collections
with > 1M vectors for best performance.
```


---

## 24. must_filters + should_filters + auto_extract (v10.0)

### 24.1 Problem with old filter model

```
OLD SearchFilters:
  filters: { application: ["SPT", "FF", ...30 apps] }

  Consumer sends 30 apps for user access scope.
  Auto extract sees "application" already provided → skips extraction.
  Result: searches ALL 30 apps even for specific question like "who owns SPT?"
  Wrong answer possible — FF owner returned instead of SPT owner ❌
```

### 24.2 New Three-Layer Model

| Layer | Field | Who sets it | When applied |
|---|---|---|---|
| Hard boundary | `must_filters` | Consumer (user permissions) | Always — no exceptions |
| Soft scope | `should_filters` | Consumer (user's allowed scope) | LLM narrows if auto_extract=true |
| Control flag | `auto_extract` | Consumer | true → LLM narrows; false → full scope |

### 24.3 Priority Chain (filter_builder.py)

```
Per field:
  1. must_filters field    → applied as-is, never overridden
  2. extracted field       → LLM narrowed within should_filters scope
                             Validated: extracted value must be in should_filters
                             If out of scope → fallback to full should_filters
  3. should_filters field  → full scope fallback if nothing extracted
```

### 24.4 access_key — per-app access control

```
Problem:
  access_group: ["general"] applies same level to ALL apps
  Cannot express: SPT=general only, FF=general+restricted

Solution:
  Computed field: access_key = "{application}::{access_group}"
  e.g. "Smart Pricing Tool::general", "Flex Forecast::restricted"

  Consumer builds per-user access_key list:
    must_filters: {
      access_key: ["Smart Pricing Tool::general",
                   "Flex Forecast::general",
                   "Flex Forecast::restricted"]
    }

  This handles ANY combination of app+level per user ✅
```

### 24.5 Files Changed

```
retrieval-service:
  schemas/search.py          → SearchFilters: must/should/auto_extract
  schemas/requests.py        → Updated SearchRequest/QueryRequest examples
  services/filter_builder.py → Priority chain implementation
  services/retrieval_service.py → auto_extract gating, should_filters scope
  services/filter_extractor.py → skip must fields, use should_filters as known_values
```

### 24.6 API Examples

```json
// DocAssist user: general only for SPT, general+restricted for FF
{
  "question": "Who is the owner of SPT?",
  "filters": {
    "must_filters": {
      "access_key": ["Smart Pricing Tool::general",
                     "Flex Forecast::general",
                     "Flex Forecast::restricted"]
    },
    "should_filters": {
      "application": ["Smart Pricing Tool", "Flex Forecast"]
    },
    "auto_extract": true
  }
}

// Explicit UI filter — user selected from dropdown
{
  "question": "Who owns SPT?",
  "filters": {
    "must_filters": {
      "access_key": ["Smart Pricing Tool::general"],
      "application": ["Smart Pricing Tool"]
    },
    "should_filters": {},
    "auto_extract": false
  }
}

// No access control — internal admin
{
  "question": "Who owns SPT?",
  "filters": {
    "must_filters": {},
    "should_filters": {},
    "auto_extract": true
  }
}
```

---

## 25. Repo-Level metadata_schema + computed Field (v10.0)

### 25.1 Problem with tenant-level schema

```
OLD: schema defined once at tenant level → applies to ALL repos
  Works when all repos have same folder structure (same connector)
  Fails when:
    - Different connectors have different path structures
    - SharePoint: domain@1, application@2, access_group@3
    - Azure Blob:  region@0, year@1, category@2
    - SQL:         no path fields at all
```

### 25.2 Solution — repo-level schema

```
Each repo defines its own metadata_schema:
  CreateRepoRequest.metadata_schema → stored on repo doc in MongoDB

Priority resolution (worker):
  1. repo.metadata_schema   → takes priority
  2. tenant.metadata_schema → fallback
  3. None                   → no custom fields

Log: task.metadata_schema_resolved source=repo|tenant has_schema=true|false
```

### 25.3 computed Field Source

```
New source type: "computed"
Derives value from other already-extracted fields via formula template.

Example:
  { field_name: access_key,
    source: computed,
    formula: "{application}::{access_group}",
    default: "unknown::unknown" }

  application  = "Smart Pricing Tool"  (extracted earlier)
  access_group = "general"             (extracted earlier)
  access_key   = "Smart Pricing Tool::general"  ← computed ✅

Rules:
  Computed fields must be LAST in custom_fields list
  References earlier fields via {field_name} placeholders
  Falls back to default if any referenced field is missing
```

### 25.4 metadata_overrides Removed

```
OLD: IngestionOverrides.metadata_overrides
  Workaround for tenant-level-only schema
  Patch specific fields for one repo
  Required tenant schema as base — confusing merge logic

REMOVED in v10.0:
  Now that each repo has its own metadata_schema,
  overrides are unnecessary — define full schema on repo directly

IngestionOverrides now contains ONLY:
  chunk_size, chunk_overlap, document_concurrency,
  contextual_enrichment_enabled
  (ingestion behaviour settings only)
```

### 25.5 Files Changed

```
ingestion-orchestrator:
  schemas/tenant.py          → computed source + formula field in CustomFieldSchema
                               MetadataSchema docstring updated (repo-level priority)
  schemas/repo_config.py     → metadata_schema added to CreateRepoRequest/PatchRepoRequest
                               metadata_overrides REMOVED from IngestionOverrides
  services/repo_service.py   → stores repo metadata_schema at creation time
  repositories/repo_config_repo.py → accepts + stores metadata_schema in repo doc

ingestion-worker:
  worker.py                  → resolves effective schema (repo > tenant priority)
                               repo_metadata_overrides removed
  services/ingestion_service.py → repo_metadata_overrides param removed
  pipeline/metadata_enricher.py → _resolve_effective_schema() removed
                                   enrich() uses schema directly (no merging)
                                   computed field source added with formula support
```

### 25.6 MongoDB — update existing repos

```javascript
// Add metadata_schema to existing repo (replaces any old overrides)
db.repos.updateOne(
  { _id: "docassist_repo_dev" },
  { $set: {
    "metadata_schema": {
      "custom_fields": [
        { "field_name": "domain",       "source": "path_segment",
          "path_segment_index": 1,      "default": "general" },
        { "field_name": "application",  "source": "path_segment",
          "path_segment_index": 2,      "default": "general" },
        { "field_name": "access_group", "source": "path_segment",
          "path_segment_index": 3,      "default": "general" },
        { "field_name": "is_general",   "source": "path_segment_equals",
          "path_segment_index": 1,      "match_value": "general",
          "true_value": "true",         "false_value": "false" },
        { "field_name": "access_key",   "source": "computed",
          "formula": "{application}::{access_group}",
          "default": "unknown::unknown" }
      ]
    },
    "ingestion_overrides.metadata_overrides": null
  }}
)

// Force re-ingest to compute access_key on all chunks
db.ingested_documents.updateMany(
  { repo_id: "docassist_repo_dev" },
  { $set: { content_hash: "force_reingest" } }
)
```
