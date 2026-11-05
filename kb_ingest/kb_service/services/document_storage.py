"""Startup-owned source/FTS migration. Retrieval never calls this module."""

import json

from .document_identity import parse_metadata
from .documents import document_metadata, lexical_text, original_document


def ensure_document_search_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE searchable_documents ADD COLUMN IF NOT EXISTS source_content TEXT;")
        cur.execute("ALTER TABLE searchable_documents ADD COLUMN IF NOT EXISTS document_meta JSONB NOT NULL DEFAULT '{}'::jsonb;")
        cur.execute("ALTER TABLE searchable_documents ADD COLUMN IF NOT EXISTS search_vector TSVECTOR;")
        cur.execute("SELECT id, source_id, content, metadata FROM searchable_documents WHERE search_vector IS NULL ORDER BY id;")
        # Backfill only missing records. Existing content, embeddings, identity,
        # original metadata and Recipe rows are never overwritten or deleted.
        while True:
            rows = cur.fetchmany(200)
            if not rows:
                break
            with conn.cursor() as writer:
                for row_id, source_id, content, raw_meta in rows:
                    metadata = parse_metadata(raw_meta)
                    raw, original = original_document(metadata)
                    try:
                        meta = document_metadata(original, str(source_id), raw)
                    except ValueError:
                        # A malformed historical date is not silently treated
                        # as a valid policy scope. Preserve raw data unchanged.
                        meta = {"document_id": str(source_id), "document_source_id": str(source_id),
                                "content_origin": "original" if raw is not None else "legacy_unverified",
                                "date_metadata_invalid": True}
                    header = lexical_text(str(source_id) + " " + str(meta.get("title") or ""))
                    body = lexical_text((raw or "") + " " + (content or ""))
                    writer.execute("""
                        UPDATE searchable_documents SET source_content=%s, document_meta=%s::jsonb,
                          search_vector=setweight(to_tsvector('simple', %s), 'A') || setweight(to_tsvector('simple', %s), 'B')
                        WHERE id=%s AND search_vector IS NULL
                    """, (raw, json.dumps(meta, ensure_ascii=False), header, body, row_id))
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sd_search_vector ON searchable_documents USING GIN (search_vector);")
    conn.commit()


def assert_document_search_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='searchable_documents'
              AND column_name IN ('source_content', 'document_meta', 'search_vector')""")
        if {row[0] for row in cur.fetchall()} != {"source_content", "document_meta", "search_vector"}:
            raise RuntimeError("Document search schema missing; restart kb_ingest to run the startup migration")
