"""Source fidelity, version identity and transactional chunk replacement."""

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kb_ingest"))
from kb_service.services.documents import document_metadata, paragraph_chunks, prepare_document_records
from kb_service.services import document_storage, vector_store
from multisource_data_agent.application.agents.tool_execution.knowledge_rag import _build_tool_result
from multisource_data_agent.application.agents.execution_contract import build_execution_dto, normalize_tool_results
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.summarize.evidence_context import build_evidence_context


def item(content="Return within 7 days. Packaging must be complete.", **extra):
    original = dict(content=content, title="Return policy", **extra)
    return dict(domain="enterprise_retail_rag_eval", dataset_id="doc-test", source_id="POL-1",
                source_table="policies", original_data=original, rewritten_content="Generated summary says 60 days.")


def test_original_policy_never_becomes_generated_summary():
    record = prepare_document_records(item())[0]
    assert record["source_content"] == item()["original_data"]["content"]
    assert "60 days" not in record["source_content"]
    assert record["_document_meta"]["content_origin"] == "original"


def test_chunks_reconstruct_original_and_keep_clause_conditions_intact():
    clause = "The eligible return window is 7 days. " + "Check packaging and accessories. " * 15 + "Exception: quality defects have 30 days.\n\n"
    raw = "# Returns\n\n" + clause + "# Refund inspection\n\n" + clause
    records = prepare_document_records(item(raw))
    assert len(records) >= 2
    assert "".join(r["source_content"] for r in records) == raw
    assert all(len(r["source_content"]) <= 900 for r in records)
    for record in records:
        if "eligible return window" in record["source_content"]:
            assert "Exception: quality defects have 30 days." in record["source_content"]
        meta = record["_document_meta"]
        assert record["source_content"] == raw[meta["start_char"]:meta["end_char"]]
        assert meta["document_source_id"] == "POL-1"


def test_oversized_single_clause_fails_explicitly_without_silent_loss():
    with pytest.raises(ValueError, match="clause exceeds"):
        prepare_document_records(item("Long clause without a paragraph boundary. " * 40))


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_paragraph_line_endings_preserve_exact_source_and_offsets(newline):
    clause = "A policy paragraph and its complete exception. " * 12
    raw = (clause + newline + " \t" + newline) * 3
    records = prepare_document_records(item(raw))
    assert len(records) == 3
    assert "".join(r["source_content"] for r in records) == raw
    for record in records:
        meta = record["_document_meta"]
        assert record["source_content"] == raw[meta["start_char"]:meta["end_char"]]
        assert len(record["source_content"]) <= 900


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_single_line_break_does_not_split_a_clause_from_its_exception(newline):
    raw = "Eligibility details. " * 30 + newline + "Exception and its conditions. " * 15
    with pytest.raises(ValueError, match="clause exceeds"):
        prepare_document_records(item(raw))


def test_explicit_versions_coexist_and_dates_are_not_guessed():
    first = prepare_document_records(item(document_version="1", effective_to="2026-09-30"))[0]
    second = prepare_document_records(item(document_version="2", effective_from="2026-10-01", scope="electronics"))[0]
    assert first["source_id"] == "POL-1@v:1" and second["source_id"] == "POL-1@v:2"
    assert second["_document_meta"]["scope"] == "electronics"
    assert "effective_from" not in prepare_document_records(item(updated_at="2026-10-01"))[0]["_document_meta"]
    with pytest.raises(ValueError, match="precedes"):
        prepare_document_records(item(effective_from="2026-10-10", effective_to="2026-10-01"))


def test_generic_row_preserves_structured_snapshot_and_legacy_is_labeled():
    row = item()
    row["original_data"] = {"customer": "Synthetic Customer", "quantity": 3}
    record = prepare_document_records(row)[0]
    assert json.loads(record["source_content"]) == row["original_data"]
    assert record["_document_meta"]["content_origin"] == "row_snapshot"
    legacy = prepare_document_records(dict(source_id="old", rewritten_content="Unverified old text"))[0]
    assert legacy["source_content"] is None
    assert legacy["_document_meta"]["content_origin"] == "legacy_unverified"


def test_evidence_keeps_exception_after_character_500_and_scope_reaches_synthesis():
    raw = "Eligibility details. " * 30 + "Exception: do not refund duplicate payments before transaction verification."
    result = _build_tool_result(question="policy", status="success", backend="pgvector", attempts=[], top_k=5,
        documents=[{"source_id": "POL-1@v:2", "content": raw, "source": "pgvector", "metadata": {
            "document_id": "POL-1", "document_version": "2", "effective_from": "2026-10-01", "scope": "electronics",
            "content_origin": "original"}}])
    assert result.evidence[0].content == raw and len(raw) > 500
    context = build_evidence_context(normalize_tool_results([result]))
    assert "do not refund duplicate payments" in context.citation_facts[0]["content"]
    assert context.citation_facts[0]["document"]["document_version"] == "2"
    assert context.citation_facts[0]["document"]["effective_from"] == "2026-10-01"


def test_legacy_oversized_context_marks_truncation_instead_of_claiming_completeness():
    result = _build_tool_result(question="old", status="success", backend="pgvector", attempts=[], top_k=5,
        documents=[{"source_id": "old", "source": "pgvector", "content": "x" * 1200}])
    assert build_evidence_context(normalize_tool_results([result])).metadata["truncated"] is True


def test_public_preview_retains_safe_document_scope_without_raw_metadata_dump():
    raw = "Condition. " * 60 + "Exception applies."
    metadata = dict(document_id="POL-1", document_source_id="POL-1@v:2", document_version="2",
                    effective_from="2026-10-01", scope="electronics", content_origin="original",
                    domain="enterprise_retail_rag_eval", chunk_index=0, original_data={"private": "not public"},
                    api_key="not public")
    result = _build_tool_result(question="policy", status="success", backend="pgvector", attempts=[], top_k=5,
        documents=[{"source_id": "POL-1@v:2", "source": "pgvector", "content": raw, "metadata": metadata}])
    normalized = normalize_tool_results([result])
    public = build_execution_dto(tool_results=normalized, route="data-query").evidence[0]
    assert public.metadata["document_version"] == "2"
    assert public.metadata["effective_from"] == "2026-10-01"
    assert public.metadata["content_origin"] == "original"
    assert "original_data" not in public.metadata and "api_key" not in public.metadata
    assert len(public.content) <= 500 and normalized[0].evidence[0].content == raw


class Cursor:
    def __init__(self, conn):
        self.conn, self.rows = conn, []

    def execute(self, sql, params=None):
        if "SELECT domain, dataset_id, source_id, content_hash" in sql:
            self.rows = [(*key, self.conn.db[key][11]) for key in params[0] if key in self.conn.db]
        elif "INSERT INTO searchable_documents" in sql:
            self.conn.db[(params[0], params[1], params[3])] = params
        elif "DELETE FROM searchable_documents" in sql:
            domain, dataset, parent, _, keep = params
            for key, row in list(self.conn.db.items()):
                meta = json.loads(row[13])
                if key[:2] == (domain, dataset) and (meta["document_source_id"] == parent or key[2] == parent) and key[2] not in keep:
                    del self.conn.db[key]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return ("searchable_documents",)


class Connection:
    def __init__(self, db):
        self.db, self.snapshot = db, copy.deepcopy(db)

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.snapshot = copy.deepcopy(self.db)

    def rollback(self):
        self.db.clear()
        self.db.update(self.snapshot)

    def close(self):
        pass


class Embedding:
    dimension = 2
    calls = 0
    fail_at = None

    def embed_texts(self, texts):
        self.calls += 1
        assert len(texts) <= 10
        self.last_texts = texts
        if self.calls == self.fail_at:
            raise RuntimeError("simulated embedding failure")
        return [[0.1, 0.2] for _ in texts]


@pytest.fixture
def writer(monkeypatch):
    db = {}
    monkeypatch.setattr(vector_store.psycopg2, "connect", lambda **kwargs: Connection(db))
    monkeypatch.setattr(vector_store, "register_vector", lambda conn: None)
    monkeypatch.setattr(vector_store, "assert_document_identity_schema", lambda conn: None)
    monkeypatch.setattr(vector_store, "assert_document_search_schema", lambda conn: None)
    instance = vector_store.VectorStoreWriter(SimpleNamespace(db_config={}, embedding_batch_size=10), Embedding())
    monkeypatch.setattr(instance, "_ensure_table", lambda *args: None)
    return instance, db


def test_repeated_ingestion_is_noop_and_embeddings_use_original(writer):
    instance, db = writer
    assert instance.upsert([item()], incremental=True) == 1
    assert "60 days" not in instance.embedding_client.last_texts[0]
    calls = instance.embedding_client.calls
    assert instance.upsert([item()], incremental=True) == 0
    assert instance.embedding_client.calls == calls and len(db) == 1


def test_first_incremental_upload_can_reach_writer_on_an_absent_table(monkeypatch):
    queries, closed = [], []
    class EmptyCursor:
        def execute(self, sql, params=None):
            queries.append(sql)
        def fetchone(self):
            return (None,)
    conn = SimpleNamespace(cursor=lambda: EmptyCursor(), close=lambda: closed.append(True))
    monkeypatch.setattr(vector_store.psycopg2, "connect", lambda **kwargs: conn)
    instance = vector_store.VectorStoreWriter(SimpleNamespace(db_config={}), Embedding())
    prepared = prepare_document_records(item())
    assert instance._filter_changed_items(prepared, instance.logger) == prepared
    assert len(queries) == 1 and "to_regclass" in queries[0]
    assert closed == [True]  # No DDL or embedding call during this read check.


def test_shortened_document_prunes_only_owned_stale_chunks(writer):
    instance, db = writer
    long = ("Rule and its complete exception. " * 18 + "\n\n") * 3
    assert instance.upsert([item(long)]) == 3
    other = item(long)
    other["dataset_id"] = "another-dataset"
    instance.upsert([other])
    instance.upsert([item("Updated shorter policy.")])
    assert len([k for k in db if k[1] == "doc-test"]) == 1
    assert len([k for k in db if k[1] == "another-dataset"]) == 3
    assert db[("enterprise_retail_rag_eval", "doc-test", "POL-1")][12] == "Updated shorter policy."


def test_new_version_never_prunes_old_version(writer):
    instance, db = writer
    instance.upsert([item(document_version="1")])
    instance.upsert([item("New rule.", document_version="2")])
    assert {key[2] for key in db} == {"POL-1@v:1", "POL-1@v:2"}


def test_failed_second_embedding_batch_rolls_back_all_chunks(writer):
    instance, db = writer
    instance.upsert([item("Previous published rule.")])
    before = copy.deepcopy(db)
    instance.embedding_client.fail_at = instance.embedding_client.calls + 2
    long = ("Rule with conditions and exceptions. " * 20 + "\n\n") * 12
    with pytest.raises(RuntimeError, match="simulated"):
        instance.upsert([item(long)])
    assert db == before


def test_search_migration_is_not_called_by_read_or_ingestion_paths():
    root = Path(__file__).resolve().parents[1]
    startup = (root / "kb_ingest/kb_service/main.py").read_text(encoding="utf-8")
    assert "ensure_document_search_schema(conn)" in startup
    for path in ("services/search.py", "services/vector_store.py"):
        code = (root / "kb_ingest/kb_service" / path).read_text(encoding="utf-8")
        assert "ensure_document_search_schema(" not in code


def test_backfill_preserves_old_content_and_labels_unrecoverable_sources():
    class MigrationCursor:
        def __init__(self, conn):
            self.conn, self.pending = conn, []
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=None):
            self.conn.sql.append(sql)
            if "SELECT id, source_id" in sql and not self.conn.done:
                self.pending = [(1, "p1", "Rewritten says 60 days.", json.dumps({"content": "Original says 7 days."})),
                                (2, "old", "No original found", "invalid metadata")]
            if "UPDATE searchable_documents SET source_content" in sql:
                self.conn.updates.append(params)
                self.conn.done = True
        def fetchmany(self, count):
            rows, self.pending = self.pending, []
            return rows
    conn = SimpleNamespace(done=False, sql=[], updates=[], commit=lambda: None)
    conn.cursor = lambda: MigrationCursor(conn)
    document_storage.ensure_document_search_schema(conn)
    assert conn.updates[0][0] == "Original says 7 days."
    assert conn.updates[1][0] is None
    assert json.loads(conn.updates[1][1])["content_origin"] == "legacy_unverified"
    assert not any("SET content =" in sql or "DELETE FROM" in sql for sql in conn.sql)
    document_storage.ensure_document_search_schema(conn)
    assert len(conn.updates) == 2  # No repeated backfill of already indexed rows.
