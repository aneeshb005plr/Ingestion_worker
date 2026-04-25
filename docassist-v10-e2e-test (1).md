# DocAssist — E2E Test Guide
**Version:** 10.0 — Apr 20 2026
**New in v10.0:**
  - must_filters + should_filters + auto_extract search API
  - Repo-level metadata_schema (priority over tenant schema)
  - Computed field support (access_key = application::access_group)
  - All consumer access scenarios

---

## What Changed in v10.0

```
Search API — new filter model:
  OLD: filters: { filters: { application: [...] }, include_general: true }
  NEW: filters: {
         must_filters:   { ... }   ← hard boundary, always applied
         should_filters: { ... }   ← soft scope, LLM narrows if auto_extract=true
         auto_extract:   true/false
       }

Repo-level metadata_schema:
  OLD: schema defined only at tenant level, same for all repos
  NEW: each repo can define its own schema
       repo.metadata_schema → priority over tenant.metadata_schema

Computed field:
  NEW source type: "computed"
  formula: "{application}::{access_group}" → "Smart Pricing Tool::general"
  Used for access_key — enables per-app access control
```

---

## Part 1 — Repo Setup with Repo-Level metadata_schema

### 1.1 — Create Tenant (minimal schema — just a template)

```json
POST /api/v1/tenants
{
  "tenant_id": "docassist_prod",
  "name": "DocAssist Production",
  "ingestion_defaults": {
    "chunk_size": 1024,
    "chunk_overlap": 150,
    "embedding_model": "text-embedding-3-small",
    "contextual_enrichment_enabled": true,
    "contextual_enrichment_mode": "all_chunks"
  },
  "api_config": {
    "genai_api_key": "<pwc-api-key>",
    "genai_base_url": "https://genai-sharedservice-americas.pwcinternal.com",
    "llm_model": "gpt-4.1-mini"
  },
  "metadata_schema": null
}
```

> Tenant has NO metadata_schema — each repo defines its own.

---

### 1.2 — Create SharePoint Repo WITH repo-level metadata_schema + computed access_key

```json
POST /api/v1/repos
{
  "repo_id": "docassist_prod_sharepoint",
  "tenant_id": "docassist_prod",
  "name": "DocAssist SharePoint XLOS",
  "source_type": "sharepoint",
  "connector_config": {
    "site_id": "<your-site-id>",
    "root_folder": "docassist-test",
    "file_extensions_include": [".pdf", ".docx", ".xlsx"]
  },
  "credentials": {
    "source_type": "sharepoint",
    "tenant_id": "<azure-tenant-id>",
    "client_id": "<client-id>",
    "client_secret": "<client-secret>"
  },
  "vector_config": {
    "index_mode": "dedicated_tenant"
  },
  "retrieval_config": {
    "filterable_fields": [
      "domain", "application", "access_group",
      "access_key", "is_general", "source_id"
    ],
    "extractable_fields": ["domain", "application"],
    "general_flag_field": "is_general",
    "hybrid_search_enabled": true,
    "hybrid_alpha": 0.7
  },
  "metadata_schema": {
    "custom_fields": [
      {
        "field_name": "domain",
        "source": "path_segment",
        "path_segment_index": 1,
        "default": "general"
      },
      {
        "field_name": "application",
        "source": "path_segment",
        "path_segment_index": 2,
        "default": "general"
      },
      {
        "field_name": "access_group",
        "source": "path_segment",
        "path_segment_index": 3,
        "default": "general"
      },
      {
        "field_name": "is_general",
        "source": "path_segment_equals",
        "path_segment_index": 1,
        "match_value": "general",
        "true_value": "true",
        "false_value": "false"
      },
      {
        "field_name": "access_key",
        "source": "computed",
        "formula": "{application}::{access_group}",
        "default": "unknown::unknown"
      }
    ]
  }
}
```

**Verify repo was created correctly:**

```javascript
db.repos.findOne(
  { _id: "docassist_prod_sharepoint" },
  { "metadata_schema": 1, "vector_config.collection_name": 1,
    "vector_config.index_name": 1 }
)

// Expected:
// metadata_schema.custom_fields → 5 fields including access_key (computed)
// vector_config.collection_name → "vector_store_docassist_prod"
// vector_config.index_name      → "vidx_tenant_docassist_prod"
```

| # | Test | Expected |
|---|---|---|
| RS1 | Repo created with repo-level schema | metadata_schema stored on repo doc |
| RS2 | computed field defined | access_key formula stored |
| RS3 | Tenant has no schema | tenant.metadata_schema = null |
| RS4 | Worker uses repo schema | task.metadata_schema_resolved source=repo |

---

### 1.3 — Create Azure Blob Repo with DIFFERENT schema

```json
POST /api/v1/repos
{
  "repo_id": "docassist_blob_finance",
  "tenant_id": "docassist_prod",
  "name": "DocAssist Azure Blob Finance",
  "source_type": "azure_blob",
  "connector_config": {
    "container_name": "finance-docs",
    "prefix": "reports/",
    "file_extensions_include": [".pdf", ".xlsx"]
  },
  "credentials": {
    "source_type": "azure_blob",
    "connection_string": "DefaultEndpointsProtocol=https;AccountName=...;"
  },
  "vector_config": {
    "index_mode": "dedicated_tenant"
  },
  "retrieval_config": {
    "filterable_fields": ["region", "year", "category", "access_key"],
    "extractable_fields": ["region", "year"],
    "hybrid_search_enabled": false
  },
  "metadata_schema": {
    "custom_fields": [
      {
        "field_name": "region",
        "source": "path_segment",
        "path_segment_index": 0,
        "default": "global"
      },
      {
        "field_name": "year",
        "source": "path_segment",
        "path_segment_index": 1,
        "default": "unknown"
      },
      {
        "field_name": "category",
        "source": "path_segment",
        "path_segment_index": 2,
        "default": "general"
      },
      {
        "field_name": "access_key",
        "source": "computed",
        "formula": "{region}::{category}",
        "default": "unknown::unknown"
      }
    ]
  }
}
```

> Different fields entirely (region/year/category) — completely different
> from SharePoint repo. Both under same tenant. Each defines own schema. ✅

| # | Test | Expected |
|---|---|---|
| RS5 | Blob repo has different schema | region, year, category fields |
| RS6 | SharePoint schema unchanged | domain, application, access_group |
| RS7 | Both repos under same tenant | vector_store_docassist_prod collection |
| RS8 | Schemas don't interfere | Each repo enriches its own docs ✅ |

---

## Part 2 — Ingestion Tests (computed access_key)

### 2.1 — Trigger Full Scan

```bash
POST /api/v1/repos/docassist_prod_sharepoint/ingest
```

**Expected logs:**
```
task.metadata_schema_resolved  source=repo  has_schema=true
sharepoint.scan_start          mode=full
document.processed             status=new chunks=47
```

**Verify access_key computed correctly:**

```javascript
// Check chunk metadata in dedicated collection
db.vector_store_docassist_prod.findOne(
  { repo_id: "docassist_prod_sharepoint" },
  {
    application: 1, domain: 1,
    access_group: 1, access_key: 1,
    is_general: 1, file_name: 1
  }
)

// For SPT_Runbook.docx (path: XLOS/Smart Pricing Tool/general/):
// Expected:
// domain:       "XLOS"
// application:  "Smart Pricing Tool"
// access_group: "general"
// access_key:   "Smart Pricing Tool::general"   ← computed ✅
// is_general:   "false"

// For SPT_Finance.docx (path: XLOS/Smart Pricing Tool/restricted/):
// access_group: "restricted"
// access_key:   "Smart Pricing Tool::restricted" ← computed ✅
```

| # | Test | Expected |
|---|---|---|
| I1 | access_key computed for general doc | "Smart Pricing Tool::general" |
| I2 | access_key computed for restricted doc | "Smart Pricing Tool::restricted" |
| I3 | access_key computed for FF general | "Flex Forecast::general" |
| I4 | access_key computed for FF restricted | "Flex Forecast::restricted" |
| I5 | is_general computed | "true" for general/ path, "false" for XLOS/ |
| I6 | Schema source logged as repo | task.metadata_schema_resolved source=repo |

---

### 2.2 — Verify Computed Field Formula Fallback

```javascript
// Check what happens when referenced field is missing/unknown
// e.g. file at root level with no application segment
// Full path: "docassist-test/CompanyPolicy.pdf"
// path_segment_index=2 → out of range → application="general"
// formula: "{application}::{access_group}" 
//   application="general", access_group="general"
//   → access_key="general::general"

db.vector_store_docassist_prod.findOne(
  { file_name: { $regex: "CompanyPolicy" } },
  { access_key: 1, application: 1, access_group: 1 }
)
// Expected: access_key = "general::general"
```

| # | Test | Expected |
|---|---|---|
| I7 | Formula with default values | "general::general" not "unknown::unknown" |
| I8 | Formula references available | access_key computed from earlier fields ✅ |

---

## Part 3 — Search API — must_filters + should_filters

### 3.1 — User A: General only for ALL apps

```json
POST /api/v1/docassist_prod/query
{
  "question": "Who is the primary owner of SPT?",
  "filters": {
    "must_filters": {
      "access_key": [
        "Smart Pricing Tool::general",
        "Flex Forecast::general",
        "Leave App::general"
      ]
    },
    "should_filters": {
      "application": [
        "Smart Pricing Tool",
        "Flex Forecast",
        "Leave App"
      ]
    },
    "auto_extract": true
  }
}
```

**Expected behaviour:**
```
auto_extract=true → LLM extracts application="Smart Pricing Tool"
Validates: "Smart Pricing Tool" in should_filters ✅
Final filter:
  access_key IN ["SPT::general", "FF::general", "LA::general"]  ← must
  AND application = "Smart Pricing Tool"                          ← narrowed

Result:
  SPT general docs only
  SPT restricted docs NEVER returned (not in access_key list)
```

| # | Test | Expected |
|---|---|---|
| SA1 | LLM extracts application from question | retrieval.filters_extracted {application: "Smart Pricing Tool"} |
| SA2 | must_filters always applied | access_key hard boundary ✅ |
| SA3 | Restricted docs not returned | No SPT::restricted in access_key |
| SA4 | Correct answer | Brad Jorgenson / Sheetal Shenoy |

---

### 3.2 — User A: Abbreviation query

```json
POST /api/v1/docassist_prod/query
{
  "question": "Who is the primary owner of SPT?",
  "filters": {
    "must_filters": {
      "access_key": ["Smart Pricing Tool::general", "Flex Forecast::general"]
    },
    "should_filters": {
      "application": ["Smart Pricing Tool", "Flex Forecast"]
    },
    "auto_extract": true
  }
}
```

```
LLM sees: application values: "Smart Pricing Tool" (SPT), "Flex Forecast" (FF)
Matches "SPT" → "Smart Pricing Tool" ✅
Same result as full name query ✅
```

| # | Test | Expected |
|---|---|---|
| SA5 | SPT abbreviation resolved | {"application": "Smart Pricing Tool"} |
| SA6 | Same answer as full name query | Brad Jorgenson ✅ |

---

### 3.3 — User B: Mixed access (restricted for some apps)

```json
POST /api/v1/docassist_prod/query
{
  "question": "Show me FF restricted data",
  "filters": {
    "must_filters": {
      "access_key": [
        "Smart Pricing Tool::general",
        "Flex Forecast::general",
        "Flex Forecast::restricted",
        "Leave App::general"
      ]
    },
    "should_filters": {
      "application": [
        "Smart Pricing Tool",
        "Flex Forecast",
        "Leave App"
      ]
    },
    "auto_extract": true
  }
}
```

```
LLM extracts: application = "Flex Forecast"
Validates: "Flex Forecast" in should_filters ✅
Final filter:
  access_key IN ["SPT::general", "FF::general",
                 "FF::restricted", "LA::general"]  ← must
  AND application = "Flex Forecast"                 ← narrowed

Result:
  FF general docs ✅
  FF restricted docs ✅   (user has FF::restricted access)
  SPT restricted NEVER  ✗ (not in access_key list)
```

| # | Test | Expected |
|---|---|---|
| SB1 | FF restricted returned for User B | FF::restricted in access_key ✅ |
| SB2 | SPT restricted NOT returned | SPT::restricted not in access_key ✅ |
| SB3 | LLM narrows to FF only | application = "Flex Forecast" not all apps |

---

### 3.4 — User C: Full restricted access

```json
POST /api/v1/docassist_prod/query
{
  "question": "Show me all SPT finance information",
  "filters": {
    "must_filters": {
      "access_key": [
        "Smart Pricing Tool::general",
        "Smart Pricing Tool::restricted",
        "Flex Forecast::general",
        "Flex Forecast::restricted"
      ]
    },
    "should_filters": {
      "application": ["Smart Pricing Tool", "Flex Forecast"]
    },
    "auto_extract": true
  }
}
```

| # | Test | Expected |
|---|---|---|
| SC1 | SPT general returned | ✅ |
| SC2 | SPT restricted returned | ✅ (user has SPT::restricted) |
| SC3 | FF general returned | ✅ |
| SC4 | FF restricted returned | ✅ |

---

### 3.5 — User D: Explicit UI filter (user selected from dropdown)

```json
POST /api/v1/docassist_prod/query
{
  "question": "Who owns SPT?",
  "filters": {
    "must_filters": {
      "access_key": [
        "Smart Pricing Tool::general",
        "Flex Forecast::general"
      ],
      "application": ["Smart Pricing Tool"]
    },
    "should_filters": {},
    "auto_extract": false
  }
}
```

```
auto_extract=false → no LLM extraction
should_filters={} → nothing to narrow
must_filters has application already set explicitly
Consumer handled narrowing → platform just applies

Final filter:
  access_key IN ["SPT::general", "FF::general"]
  AND application IN ["Smart Pricing Tool"]
```

| # | Test | Expected |
|---|---|---|
| SD1 | auto_extract=false → no LLM call | retrieval.auto_extract_skipped logged |
| SD2 | Explicit application applied | application = "Smart Pricing Tool" from must |
| SD3 | No double extraction | LLM not called ✅ |

---

### 3.6 — User E: No access control (internal/admin)

```json
POST /api/v1/docassist_prod/query
{
  "question": "Who owns SPT?",
  "filters": {
    "must_filters": {},
    "should_filters": {},
    "auto_extract": true
  }
}
```

```
must_filters={} → no hard boundary
should_filters={} → no scope
auto_extract=true but no should_filters → nothing to extract from

Only tenant_id + repo_id always applied (platform default)
LLM extraction skipped (no should_filters to narrow)
Falls back to extracting from vector_store known values
Narrows to SPT if detected
```

| # | Test | Expected |
|---|---|---|
| SE1 | No access restriction | All docs in tenant scope searched |
| SE2 | LLM still extracts from known values | application = "Smart Pricing Tool" |
| SE3 | Correct answer | Brad Jorgenson ✅ |

---

### 3.7 — Vague question (no specific app mentioned)

```json
POST /api/v1/docassist_prod/query
{
  "question": "What documents do we have?",
  "filters": {
    "must_filters": {
      "access_key": [
        "Smart Pricing Tool::general",
        "Flex Forecast::general",
        "Leave App::general"
      ]
    },
    "should_filters": {
      "application": ["Smart Pricing Tool", "Flex Forecast", "Leave App"]
    },
    "auto_extract": true
  }
}
```

```
LLM detects nothing specific from question
extracted = {}
should_filters applied as full scope:
  application IN ["Smart Pricing Tool", "Flex Forecast", "Leave App"]
AND access_key IN [general keys only]

Searches all 3 apps ✅ (correct — user asked generally)
```

| # | Test | Expected |
|---|---|---|
| SV1 | Vague question → full scope | application IN [3 apps] |
| SV2 | access_key still enforced | restricted docs not returned |
| SV3 | No LLM extraction | retrieval.filters_extracted not logged |

---

## Part 4 — Repo-Level Schema Priority Tests

### 4.1 — Worker uses repo schema over tenant schema

```javascript
// Setup: tenant has schema, repo also has schema
// Confirm repo schema wins

// Update tenant to have a schema (simulate old setup)
db.tenants.updateOne(
  { _id: "docassist_prod" },
  { $set: {
    "metadata_schema": {
      "custom_fields": [
        {
          "field_name": "tenant_field",
          "source": "static",
          "default": "from_tenant"
        }
      ]
    }
  }}
)

// Trigger ingestion — repo schema should win
POST /api/v1/repos/docassist_prod_sharepoint/ingest
```

**Expected log:**
```
task.metadata_schema_resolved  source=repo  has_schema=true
```

**Verify:**
```javascript
// Check chunk — should have repo fields (domain, application, access_key)
// NOT tenant_field (from tenant schema)
db.vector_store_docassist_prod.findOne(
  { repo_id: "docassist_prod_sharepoint" },
  { tenant_field: 1, domain: 1, application: 1, access_key: 1 }
)
// Expected:
// tenant_field: null  ← NOT present (repo schema took priority) ✅
// domain: "XLOS"     ← from repo schema ✅
// access_key: "Smart Pricing Tool::general"  ← computed ✅
```

| # | Test | Expected |
|---|---|---|
| P1 | Repo schema takes priority | source=repo in logs |
| P2 | tenant_field not in chunks | Repo schema, not tenant |
| P3 | Repo fields correct | domain/application/access_key from repo |

---

### 4.2 — Fallback to tenant schema when repo has none

```javascript
// Create repo with NO metadata_schema
// Tenant has a schema
// Worker should fall back to tenant schema

// First ensure tenant has schema
db.tenants.updateOne(
  { _id: "docassist_prod" },
  { $set: {
    "metadata_schema": {
      "custom_fields": [
        { "field_name": "domain", "source": "path_segment",
          "path_segment_index": 1, "default": "general" }
      ]
    }
  }}
)

// Create repo with no schema
POST /api/v1/repos
{
  "repo_id": "docassist_no_schema_repo",
  "tenant_id": "docassist_prod",
  ...
  "metadata_schema": null   ← no repo-level schema
}

// Trigger ingestion
POST /api/v1/repos/docassist_no_schema_repo/ingest
```

**Expected log:**
```
task.metadata_schema_resolved  source=tenant  has_schema=true
```

| # | Test | Expected |
|---|---|---|
| P4 | No repo schema → use tenant | source=tenant in logs |
| P5 | Tenant fields extracted | domain field present in chunks |

---

### 4.3 — No schema at all

```javascript
// Both tenant and repo have no schema
db.tenants.updateOne({ _id: "docassist_prod" },
  { $unset: { "metadata_schema": "" } })

POST /api/v1/repos/docassist_no_schema_repo/ingest
```

**Expected log:**
```
task.metadata_schema_resolved  source=tenant  has_schema=false
```

| # | Test | Expected |
|---|---|---|
| P6 | No schema anywhere | has_schema=false |
| P7 | No custom fields on chunks | Only system fields (tenant_id, repo_id, etc.) |
| P8 | Search still works | No filtering by custom fields ✅ |

---

## Part 5 — computed Field Tests

### 5.1 — access_key values in Atlas

```javascript
// Check all distinct access_keys after ingestion
db.vector_store_docassist_prod.distinct("access_key", {
  repo_id: "docassist_prod_sharepoint"
})

// Expected:
// [
//   "Smart Pricing Tool::general",
//   "Smart Pricing Tool::restricted",
//   "Flex Forecast::general",
//   "Flex Forecast::restricted",
//   "Leave App::general",
//   "general::general"   ← root-level general docs
// ]
```

### 5.2 — Atlas index must include access_key

```
Vector index definition on vector_store_docassist_prod must include:
  access_key as filterable field

Atlas index filter fields:
  domain:       string
  application:  string
  access_group: string
  access_key:   string   ← NEW — must be added
  is_general:   string
  source_id:    string
```

| # | Test | Expected |
|---|---|---|
| CF1 | access_key in Atlas index | Field indexed for filtering |
| CF2 | Filter by access_key works | Atlas returns correct chunks |
| CF3 | All combinations present | 6 distinct access_key values |

---

## Part 6 — Filter Extractor with should_filters

### 6.1 — LLM uses should_filters as known_values

```
When auto_extract=true and should_filters has values:
  LLM receives ONLY the values from should_filters as known_values
  NOT all values from vector_store

  This is MORE ACCURATE because:
    vector_store might have apps user can't access
    should_filters = exactly what this user can see
    LLM can only extract values within user's scope

  e.g. User's should_filters.application = ["SPT", "FF"]
       LLM receives: application values: "Smart Pricing Tool" (SPT), "Flex Forecast" (FF)
       NOT all apps in vector_store (user might not have access to all)
```

**Verify via logs:**

```
filter_extractor.extracted
  question="Who owns SPT?"
  extracted={"application": "Smart Pricing Tool"}
```

| # | Test | Expected |
|---|---|---|
| FE1 | Known values from should_filters | LLM sees only user's allowed apps |
| FE2 | Abbreviation works | SPT → Smart Pricing Tool ✅ |
| FE3 | Out-of-scope extraction skipped | Value not in should_filters → full scope |

---

### 6.2 — Extraction out of scope

```json
POST /api/v1/docassist_prod/query
{
  "question": "Tell me about Finance App",
  "filters": {
    "must_filters": {
      "access_key": ["Smart Pricing Tool::general", "Flex Forecast::general"]
    },
    "should_filters": {
      "application": ["Smart Pricing Tool", "Flex Forecast"]
    },
    "auto_extract": true
  }
}
```

```
LLM tries to extract "Finance App" from question
"Finance App" NOT in should_filters (user can't see it)
→ validation fails → extraction dropped
→ fall back to full should_filters scope:
   application IN ["Smart Pricing Tool", "Flex Forecast"]

No Finance App chunks returned (not in access_key either) ✅
Correct: user asking about something they can't access
```

| # | Test | Expected |
|---|---|---|
| FE4 | Out-of-scope app extracted | filter_builder.extraction_out_of_scope logged |
| FE5 | Full scope applied as fallback | application IN [SPT, FF] |
| FE6 | Finance App not returned | Not in access_key → 0 results |

---

## Part 7 — Summary Checklist

### Repo-Level metadata_schema
| # | Test | Expected |
|---|---|---|
| RS1-RS4 | Repo schema stored + used | source=repo in worker logs |
| RS5-RS8 | Different repos have different schemas | No interference ✅ |
| P1-P3 | Repo schema priority | tenant schema ignored when repo has own |
| P4-P5 | Tenant fallback | Used when repo has no schema |
| P6-P8 | No schema anywhere | Works fine, no custom fields |

### Computed Field (access_key)
| # | Test | Expected |
|---|---|---|
| I1-I4 | access_key computed correctly | "App::access_group" format |
| I5-I6 | is_general computed | true/false based on path |
| CF1-CF3 | Atlas index includes access_key | Filter works ✅ |

### must_filters + should_filters
| # | Test | Expected |
|---|---|---|
| SA1-SA6 | User A: general only, SPT query | Correct answer, no restricted |
| SB1-SB3 | User B: mixed access | FF restricted returned, SPT restricted not |
| SC1-SC4 | User C: full restricted | All docs returned |
| SD1-SD3 | User D: explicit UI filter | auto_extract=false, must_filters applied |
| SE1-SE3 | User E: no restriction | All docs, still extracts from question |
| SV1-SV3 | Vague question | Full scope applied, no extraction |

### Filter Extractor
| # | Test | Expected |
|---|---|---|
| FE1-FE3 | Known values from should_filters | Scoped to user's allowed apps |
| FE4-FE6 | Out-of-scope extraction | Graceful fallback to full scope |

---

## Part 8 — MongoDB Setup for access_key

### Update existing repos to add access_key field

```javascript
// For existing repos — add access_key to metadata_schema
db.repos.updateOne(
  { _id: "docassist_repo_dev" },
  { $set: {
    "metadata_schema": {
      "custom_fields": [
        { "field_name": "domain", "source": "path_segment",
          "path_segment_index": 1, "default": "general" },
        { "field_name": "application", "source": "path_segment",
          "path_segment_index": 2, "default": "general" },
        { "field_name": "access_group", "source": "path_segment",
          "path_segment_index": 3, "default": "general" },
        { "field_name": "is_general", "source": "path_segment_equals",
          "path_segment_index": 1, "match_value": "general",
          "true_value": "true", "false_value": "false" },
        { "field_name": "access_key", "source": "computed",
          "formula": "{application}::{access_group}",
          "default": "unknown::unknown" }
      ]
    }
  }}
)

// Force re-ingest to compute access_key on all existing chunks
db.ingested_documents.updateMany(
  { repo_id: "docassist_repo_dev" },
  { $set: { content_hash: "force_reingest" } }
)
```

---

## Part 9 — Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| access_key not on chunks | Computed field not in schema | Add computed field to repo metadata_schema |
| access_key = "unknown::unknown" | Referenced field missing/unknown | Check path_segment_index for application + access_group |
| Filter returns 0 results | access_key not in Atlas index | Add access_key to Atlas index filterable fields |
| source=tenant in logs | Repo has no metadata_schema | Add metadata_schema to repo |
| auto_extract not working | should_filters empty | Populate should_filters with user's allowed values |
| Wrong app extracted | Extracted value not in should_filters | Validation catches it → falls back to full scope |
| Restricted docs returned | access_key not in must_filters | Move access control to must_filters |
| retrieval.auto_extract_skipped | auto_extract=false | Intentional — consumer set it |
| LLM extracts wrong app | App not in should_filters scope | Consumer should include all user's apps in should_filters |

---

## Part 10 — metadata_overrides Removal Tests

### 10.1 — Verify metadata_overrides no longer accepted in API

```json
POST /api/v1/repos
{
  "repo_id": "test_overrides_repo",
  "tenant_id": "docassist_prod",
  ...
  "ingestion_overrides": {
    "chunk_size": 512,
    "metadata_overrides": [...]   ← should be ignored/rejected
  },
  "metadata_schema": {
    "custom_fields": [...]        ← this is the correct place now
  }
}
```

| # | Test | Expected |
|---|---|---|
| MO1 | metadata_overrides in ingestion_overrides | Field ignored (not in schema) |
| MO2 | metadata_schema in CreateRepoRequest | Accepted and stored ✅ |
| MO3 | ingestion_overrides only has behaviour fields | chunk_size, chunk_overlap, concurrency, enrichment |

---

### 10.2 — Verify worker no longer reads metadata_overrides

```bash
POST /api/v1/repos/docassist_prod_sharepoint/ingest

# Expected logs — should NOT contain:
#   metadata_enricher.field_overridden  ← old override log (removed)

# Expected logs — SHOULD contain:
#   task.metadata_schema_resolved  source=repo  has_schema=true
```

| # | Test | Expected |
|---|---|---|
| MO4 | No field_overridden log | merge logic removed ✅ |
| MO5 | metadata_schema_resolved logged | source=repo or tenant |
| MO6 | All custom fields extracted | domain, application, access_group, access_key |

---

### 10.3 — Existing repos with old metadata_overrides in MongoDB

```javascript
// Old repos may have metadata_overrides in ingestion_overrides
// Worker no longer reads it — safe to leave or clean up

// Clean up old overrides (optional):
db.repos.updateMany(
  { "ingestion_overrides.metadata_overrides": { $exists: true } },
  { $unset: { "ingestion_overrides.metadata_overrides": "" } }
)

// Migrate old overrides to repo metadata_schema:
// Manually inspect each repo, build metadata_schema from their overrides
// Then set repo.metadata_schema and force re-ingest
```

| # | Test | Expected |
|---|---|---|
| MO7 | Old repo with metadata_overrides | Worker ignores it, uses effective_metadata_schema |
| MO8 | Worker falls back to tenant schema | If repo has no metadata_schema |
| MO9 | After migration to repo schema | source=repo in logs ✅ |
