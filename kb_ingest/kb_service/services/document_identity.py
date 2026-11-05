"""Stable identity helpers for pgvector documents.

The ingestion service historically used the uploaded filename as ``source_table``
and keyed rows by ``(source_table, source_id)``.  This module keeps those fields
for compatibility while making the business identity explicit and stable.
"""

from __future__ import annotations

import hashlib
import json
import re
import logging
from typing import Any, Dict, Mapping, Optional, Tuple


DEFAULT_ENTERPRISE_DOMAIN = "enterprise_retail"
DEFAULT_ENTERPRISE_DATASET = "enterprise_retail_knowledge_v1"
LEGACY_RECIPE_DOMAIN = "recipe"
LEGACY_RECIPE_DATASET = "recipe_legacy_v1"
LEGACY_DOMAIN = "legacy"

_UUID_PREFIX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}_",
    re.IGNORECASE,
)


def parse_metadata(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def stable_upload_dataset_id(
    content: bytes,
    *,
    filename: Optional[str] = None,
    prefix: str = "upload",
) -> str:
    """Return a stable id for known or content-addressed uploads.

    Known Enterprise demo filenames map to fixed logical dataset ids. Unknown
    uploads use a content hash, so identical bytes share an immutable id while
    changed bytes receive a new id. Callers that need in-place updates for a
    logical dataset must provide an explicit stable ``dataset_id``.
    """

    if filename:
        stem = _UUID_PREFIX.sub("", filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        stem = stem.rsplit(".", 1)[0].strip().lower()
        if stem == "knowledge_base":
            return DEFAULT_ENTERPRISE_DATASET
        if stem == "weekly_campaign":
            return "enterprise_retail_weekly_campaign_v1"

    digest = hashlib.sha256(content).hexdigest()[:24]
    return f"{prefix}_{digest}"


def assert_document_identity_schema(conn: Any) -> None:
    """Verify the startup migration installed the identity contract.

    This function is deliberately read-only. DDL, legacy backfill, duplicate
    cleanup and index creation belong to the service startup migration.
    """

    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'searchable_documents'
          AND column_name IN ('domain', 'dataset_id', 'source_id')
        """
    )
    columns = {row[0] for row in cur.fetchall()}
    missing = {'domain', 'dataset_id', 'source_id'} - columns
    if missing:
        raise RuntimeError(
            "searchable_documents identity schema is not ready; "
            f"missing columns: {', '.join(sorted(missing))}"
        )

    cur.execute(
        """
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'searchable_documents'
          AND indexname = 'idx_sd_domain_dataset_source'
        """
    )
    if cur.fetchone() is None:
        raise RuntimeError(
            "searchable_documents identity schema is not ready; "
            "missing idx_sd_domain_dataset_source"
        )


def stable_source_id(row: Mapping[str, Any], row_index: int) -> str:
    """Prefer a business key, then use a deterministic row position."""

    for key in ("source_id", "doc_id", "document_id", "id", "sku", "record_id"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return str(row_index)


def infer_legacy_identity(source_table: Optional[str], metadata: Any) -> Tuple[str, str]:
    """Map pre-domain rows to stable compatibility identities."""

    table = str(source_table or "").strip()
    meta = parse_metadata(metadata)
    explicit_domain = str(meta.get("domain") or "").strip()
    explicit_dataset = str(meta.get("dataset_id") or "").strip()
    if explicit_domain:
        return explicit_domain, explicit_dataset or table or LEGACY_DOMAIN

    table_lower = table.lower()
    has_recipe_metadata = any(
        key in meta for key in ("菜品名称", "菜系", "食材", "历史故事", "recipe_id")
    )
    if table == "Sheet1" or has_recipe_metadata:
        return LEGACY_RECIPE_DOMAIN, explicit_dataset or LEGACY_RECIPE_DATASET

    if table_lower.endswith("_knowledge_base") or table_lower.endswith("_weekly_campaign"):
        if table_lower.endswith("_knowledge_base"):
            return DEFAULT_ENTERPRISE_DOMAIN, explicit_dataset or DEFAULT_ENTERPRISE_DATASET
        return DEFAULT_ENTERPRISE_DOMAIN, explicit_dataset or "enterprise_retail_weekly_campaign_v1"

    if any(str(meta.get(key) or "").startswith("ENT-") for key in ("doc_id", "document_id")):
        return DEFAULT_ENTERPRISE_DOMAIN, explicit_dataset or DEFAULT_ENTERPRISE_DATASET

    return LEGACY_DOMAIN, explicit_dataset or table or LEGACY_DOMAIN


def resolve_item_identity(item: Mapping[str, Any]) -> Tuple[str, str, str, Dict[str, Any]]:
    """Resolve and normalize an ingestion item without changing its content."""

    metadata = parse_metadata(item.get("original_data") or item.get("metadata"))
    source_table = str(item.get("source_table") or metadata.get("source_table") or "legacy")
    inferred_domain, inferred_dataset = infer_legacy_identity(source_table, metadata)
    domain = str(item.get("domain") or metadata.get("domain") or inferred_domain)
    dataset_id = str(item.get("dataset_id") or metadata.get("dataset_id") or inferred_dataset)
    source_id = str(item.get("source_id") or metadata.get("source_id") or "0")
    metadata.update({"domain": domain, "dataset_id": dataset_id, "source_id": source_id})
    return domain, dataset_id, source_id, metadata


def ensure_document_identity_schema(conn: Any, logger: Optional[logging.Logger] = None) -> None:
    """Upgrade an existing searchable_documents table in place.

    The migration is intentionally idempotent and does not remove Recipe rows.
    Older UUID-prefixed Enterprise uploads that represent the same source are
    coalesced before the new identity index is created.
    """

    log = logger or logging.getLogger(__name__)
    cur = conn.cursor()
    cur.execute("ALTER TABLE searchable_documents ADD COLUMN IF NOT EXISTS domain TEXT;")
    cur.execute("ALTER TABLE searchable_documents ADD COLUMN IF NOT EXISTS dataset_id TEXT;")
    cur.execute("ALTER TABLE searchable_documents ALTER COLUMN domain SET DEFAULT 'legacy';")
    cur.execute("ALTER TABLE searchable_documents ALTER COLUMN dataset_id SET DEFAULT 'legacy';")
    cur.execute(
        """
        UPDATE searchable_documents
        SET domain = CASE
            WHEN source_table = 'Sheet1' THEN 'recipe'
            WHEN lower(COALESCE(source_table, '')) LIKE '%_knowledge_base'
              OR lower(COALESCE(source_table, '')) LIKE '%_weekly_campaign'
              OR source_table LIKE 'enterprise_retail%%' THEN 'enterprise_retail'
            ELSE COALESCE(NULLIF(domain, ''), 'legacy')
        END
        WHERE domain IS NULL OR domain = '';
        """
    )
    cur.execute(
        """
        UPDATE searchable_documents
        SET dataset_id = CASE
            WHEN domain = 'recipe' THEN 'recipe_legacy_v1'
            WHEN domain = 'enterprise_retail'
             AND lower(COALESCE(source_table, '')) LIKE '%_weekly_campaign'
                THEN 'enterprise_retail_weekly_campaign_v1'
            WHEN domain = 'enterprise_retail' THEN 'enterprise_retail_knowledge_v1'
            ELSE COALESCE(NULLIF(dataset_id, ''), NULLIF(source_table, ''), 'legacy')
        END
        WHERE dataset_id IS NULL OR dataset_id = '';
        """
    )
    # Older Enterprise rows used a numeric row offset even when the source
    # already carried a stable document/SKU key.  Align those legacy rows with
    # the key chosen by the current processor before deduplication.
    cur.execute(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_name = 'searchable_documents' AND column_name = 'metadata'
        LIMIT 1;
        """
    )
    metadata_type = cur.fetchone()
    if metadata_type and metadata_type[0] == "jsonb":
        cur.execute(
            """
            UPDATE searchable_documents
            SET source_id = COALESCE(
                NULLIF(metadata->>'source_id', ''),
                NULLIF(metadata->>'doc_id', ''),
                NULLIF(metadata->>'document_id', ''),
                NULLIF(metadata->>'id', ''),
                NULLIF(metadata->>'sku', ''),
                source_id
            )
            WHERE domain = 'enterprise_retail';
            """
        )
    cur.execute("DROP INDEX IF EXISTS idx_sd_source_pair;")
    cur.execute(
        """
        DO $$
        DECLARE constraint_name TEXT;
        BEGIN
            SELECT con.conname INTO constraint_name
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            WHERE rel.relname = 'searchable_documents'
              AND con.contype = 'u'
              AND con.conkey = ARRAY[
                (SELECT attnum FROM pg_attribute WHERE attrelid = rel.oid AND attname = 'source_table'),
                (SELECT attnum FROM pg_attribute WHERE attrelid = rel.oid AND attname = 'source_id')
              ]
            LIMIT 1;
            IF constraint_name IS NOT NULL THEN
                EXECUTE format('ALTER TABLE searchable_documents DROP CONSTRAINT %I', constraint_name);
            END IF;
        END $$;
        """
    )
    cur.execute(
        """
        DELETE FROM searchable_documents older
        USING searchable_documents newer
        WHERE older.id < newer.id
          AND older.domain = newer.domain
          AND older.dataset_id = newer.dataset_id
          AND older.source_id = newer.source_id;
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sd_domain_dataset_source
        ON searchable_documents (domain, dataset_id, source_id);
        """
    )
    cur.execute("ALTER TABLE searchable_documents ALTER COLUMN domain SET NOT NULL;")
    cur.execute("ALTER TABLE searchable_documents ALTER COLUMN dataset_id SET NOT NULL;")
    conn.commit()
    log.info("searchable_documents identity migration ensured")


__all__ = [
    "DEFAULT_ENTERPRISE_DATASET",
    "DEFAULT_ENTERPRISE_DOMAIN",
    "LEGACY_DOMAIN",
    "LEGACY_RECIPE_DATASET",
    "LEGACY_RECIPE_DOMAIN",
    "infer_legacy_identity",
    "ensure_document_identity_schema",
    "assert_document_identity_schema",
    "parse_metadata",
    "resolve_item_identity",
    "stable_source_id",
    "stable_upload_dataset_id",
]
