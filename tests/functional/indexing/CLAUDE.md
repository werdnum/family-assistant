# Indexing and Document Processing Testing Guide

End-to-end tests for the document/email/note indexing pipeline, in `tests/functional/indexing/`.

## Test Files

- `test_document_basic.py` / `test_document_advanced.py` / `test_document_retrieval.py` — document
  ingestion, extraction, and vector retrieval.
- `test_email_basic.py` / `test_email_advanced.py` / `test_email_attachments.py` — email parsing,
  threads, and attachment extraction.
- `test_notes_basic.py` / `test_notes_search.py` — note indexing and search.
- `test_indexing_pipeline.py` — the pipeline itself; also the home of the pipeline fixtures below.
- `test_indexing_events.py` — events emitted during indexing.
- `test_reindexing.py` — re-indexing already-stored content.
- `processors/test_llm_intelligence_processor.py` — the LLM intelligence content processor.

Sample fixtures files live in `tests/data/` (`test_doc.md`, `test_doc.pdf`).

## Fixtures

`mock_pipeline_embedding_generator` and `indexing_task_worker` are defined **locally in
`test_indexing_pipeline.py`**, not in a conftest — import or re-declare them if you need them
elsewhere. `mock_pipeline_embedding_generator` returns a `HashingWordEmbeddingGenerator`, so
embeddings are deterministic for a given input. `indexing_task_worker` yields
`(TaskWorker, new_task_event, shutdown_event)`.

Other local fixtures in this directory: `mock_embedding_generator`,
`mock_embedding_generator_notes`, `temp_text_file`, `http_client`.

Database: `db_engine` for most tests, `pg_vector_db_engine` where real vector search is under test.

## Running

```bash
pytest tests/functional/indexing/ -xq
pytest tests/functional/indexing/ --postgres -xq
```

pgvector indexes are not created on the SQLite backend, so run vector-search tests with `--postgres`
when index behaviour (not just correctness) matters.

## See Also

- **[tests/CLAUDE.md](../../CLAUDE.md)** — general testing patterns and fixtures
- **[tests/integration/CLAUDE.md](../../integration/CLAUDE.md)** — integration testing with VCR.py
- **[src/family_assistant/tools/CLAUDE.md](../../../src/family_assistant/tools/CLAUDE.md)** —
  indexing tool development
- `tests/functional/vector_search/` — similarity-search coverage
