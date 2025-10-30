# `dict[str, Any]` Usage Catalog and Typing Recommendations

## Overview

This catalog documents how `dict[str, Any]` is currently employed across the codebase, organized by
feature domain. Each entry captures the core use case, notable call sites, and recommended paths
toward stronger typing (e.g., `TypedDict`, dataclasses, or Pydantic models). The inventory reflects
the audit performed during the earlier investigation.

## 1. Document Ingestion & Indexing

### 1.1 Document models and metadata

- **`IngestedDocument`** wraps a dictionary with fixed keys such as `_source_type`, `_source_id`,
  `_title`, etc., before the payload is handed to the vector store. A frozen dataclass or
  `TypedDict` that implements the document protocol would make construction and downstream usage
  type-safe.
- **`EmailDocument`** stores `_base_metadata` and `_attachment_info_raw` as raw dict/list-of-dicts
  even though the allowed keys are enumerated when the object is built from a database row.
  Converting these to typed dataclasses/TypedDicts (e.g., `EmailMetadata`, `EmailAttachmentInfo`)
  would surface mistakes when new metadata is introduced.
- **`IndexableContent.metadata`** carries structured processor details (chunk indices, original
  keys, filenames). Defining a union of `TypedDict`s for the known metadata shapes (chunking, URL
  scraping, raw file info) would help processors share a contract.

### 1.2 Task payloads and inter-service messages

- Ingestion queues a **`task_payload`** containing known keys like `document_id`, `content_parts`,
  `file_ref`, etc. Similar patterns appear in `DocumentIndexer.process_document`,
  `EmailIndexer.handle_index_email`, `NotesIndexer.handle_index_note`, and
  `handle_embed_and_store_batch`, each expecting specific fields inside `payload` dictionaries.
  Replacing these with shared `TypedDict` definitions (e.g., `ProcessDocumentTask`,
  `EmbedBatchPayload`) would eliminate repeated runtime key checks and align the producer/consumer
  expectations.
- The **`embedding_metadata_list`** handed to the embedding worker has a precise structure
  (embedding type, chunk index, metadata). A dedicated typed model would prevent silent schema drift
  between processors and the storage layer.

### 1.3 Pipeline configuration

- **`DocumentIndexer`** interprets a `pipeline_config` of processor entries where each item must
  contain `type` and an optional nested `config` dict. Unknown shapes are logged and skipped.
  Introducing Pydantic models (one per processor) with discriminated unions would validate
  configuration earlier and document the available processor options.
- **`IndexingPipeline`** accepts a free-form `config` dict that is expected to contain keys like
  `text_chunker.embedding_type_prefix_map`. A typed configuration object shared between the pipeline
  and processors would remove the need for repeated `isinstance(..., dict)` guards.

### 1.4 Event emission metadata

- When emitting `DOCUMENT_READY` events, metadata dictionaries hold known counts and identifiers. A
  `TypedDict` for indexing events would make sure downstream listeners receive the documented keys.

## 2. Calendar Integration

- **`fetch_upcoming_events`** accepts a `calendar_config: dict[str, Any]` even though
  `CalendarConfig`/`CalDavConfig` TypedDicts already exist under `family_assistant.tools.types`.
  Switching the function signature to those types would immediately validate configuration
  completeness.
- Normalized **calendar event objects** returned from CalDAV/iCal merges are dicts with stable keys
  (`summary`, `start`, `end`, etc.), yet helpers like `get_sort_key_caldav` and
  `format_events_for_prompt` rely on dynamic indexing. A `TypedDict` (e.g., `CalendarEvent`) would
  prevent missing-field errors and document the expected mix of `date`/`datetime` types.
- Prompt formatting takes **`prompts: dict[str, str]`** but only reads a handful of keys; a
  `TypedDict` with defaults (or a small dataclass) could clarify which template strings must be
  present.

## 3. Starlark Scripting Bridge

### 3.1 Candidates for stronger typing

- The **time API** serializes `datetime` objects to dictionaries with well-defined fields (year,
  month, timezone, unix, etc.) and accepts the same shape back in `_dict_to_datetime` and exported
  helpers. Introducing a `TimeDict` `TypedDict` (and a `DurationDict` if needed) would let both the
  Starlark bridge and Python tooling share an explicit schema.
- **`wake_llm`** queues dictionaries containing `context` (message, attachments) and
  `include_event`; consumers assume both keys exist. Defining `TypedDict`s for `WakeLLMRequest` and
  `WakeLLMContext` (with a typed list of attachment IDs) would avoid malformed wake requests and
  simplify unit testing.
- **Attachment metadata** returned by `AttachmentAPI.get/list` has a stable shape of attachment IDs,
  MIME types, sizes, etc. Converting the registry’s return values to typed dataclasses/TypedDicts
  (shared with the storage layer) would better document cross-service attachment fields.

### 3.2 Intentionally dynamic surfaces

- **`StarlarkEngine.evaluate`** accepts a `globals_dict` that injects arbitrary host objects or
  callables into the script sandbox; forcing a typed structure would defeat the extension mechanism,
  so keeping it dynamic is appropriate.
- **Tool definitions and parameters** come directly from tool providers (often mirroring OpenAI
  function schemas). `ToolInfo.parameters`, cached `_tool_definitions`, and JSON arguments
  intentionally mirror third-party JSON schema and need to remain flexible unless the upstream
  schema is formalized elsewhere. Wrapping them in Pydantic models is possible but would need to
  mirror the whole JSON Schema specification; the current approach is pragmatic.

## 4. Developer Tooling Scripts

- **`CodeReviewToolbox.review_data`** accumulates review findings in a dictionary, but the rest of
  the script expects specific keys when formatting results. A small `TypedDict` (e.g., `ReviewData`
  with `summary`, `issues`, `submitted`) would remove repeated `.get(...)` calls and catch
  accidental field renames.
- **`prepare_test_cases`** groups tool-call fixtures into `dict[str, list[dict[str, Any]]]`, yet
  every test case shares the same keys (`tool_call`, `tool_response`, `timestamp`). Defining a
  `TypedDict` for the case payload (and maybe another for minimal anonymized tool calls) would make
  the anonymization/serialization pipeline safer.

## Summary of Recommendations

- **Strong candidates for TypedDict/dataclass/Pydantic**: ingestion/task payloads, document metadata
  models, indexing configuration, calendar configuration & events, time API structures, wake_llm
  request objects, attachment metadata, review/test data scripts.
- **Likely to remain dynamic**: Starlark global bindings and tool JSON schema surfaces where
  flexibility for arbitrary keys is required.
