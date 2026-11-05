
import json
import logging
from typing import Iterable, List, Optional

import psycopg2
from pgvector.psycopg2 import register_vector

from kb_service.clients.embedding import EmbeddingClient
from kb_service.core.config import Config
from kb_service.services.utils import compute_content_hash
from kb_service.services.document_identity import (
    assert_document_identity_schema,
    resolve_item_identity,
)
from kb_service.services.documents import lexical_text, prepare_document_records
from kb_service.services.document_storage import assert_document_search_schema


class VectorStoreWriter:
    """Handles embedding generation and upserts into pgvector-backed tables."""

    def __init__(self, config: Config, embedding_client: Optional[EmbeddingClient] = None):
        self.config = config
        self.embedding_client = embedding_client or EmbeddingClient(config)
        self.logger = logging.getLogger(__name__)

    def upsert(
        self,
        items: Iterable[dict],
        logger: Optional[logging.Logger] = None,
        incremental: bool = False
    ) -> int:
        items = [record for item in items for record in prepare_document_records(item)]
        if not items:
            (logger or self.logger).info("没有可嵌入的数据，跳过。")
            return 0

        log = logger or self.logger
        groups = {}
        for item in items:
            if item.get("_document_group"):
                groups.setdefault(item["_document_group"], []).append(item["source_id"])

        # 增量模式：过滤出需要处理的数据
        if incremental:
            items = self._filter_changed_items(items, log)
            if not items:
                log.info("增量模式：所有数据均未变化，跳过处理。")
                return 0
            log.info("增量模式：检测到 %s 条数据需要更新", len(items))

        vector_dim = int(getattr(self.embedding_client, "dimension", 0) or 0)
        if vector_dim <= 0:
            probe_vec = self.embedding_client.embed_texts(["__probe__"])[0]
            vector_dim = len(probe_vec)
            self.embedding_client.dimension = vector_dim

        conn = psycopg2.connect(**self.config.db_config)
        try:
            cur = conn.cursor()
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            conn.commit()
            register_vector(conn)

            self._ensure_table(conn, vector_dim, log)
            assert_document_identity_schema(conn)
            assert_document_search_schema(conn)

            batch_size = max(1, int(getattr(self.config, "embedding_batch_size", 10)))
            total = 0
            for start in range(0, len(items), batch_size):
                batch = items[start:start + batch_size]
                texts = [
                    item["_document_meta"].get("title", "") + "\n" + item["source_content"]
                    if item.get("source_content") is not None else item.get("rewritten_content") or ""
                    for item in batch
                ]
                embeddings = self.embedding_client.embed_texts(texts)
                if len(embeddings) != len(batch):
                    raise ValueError("Embedding response count does not match the document batch")

                for idx, embedding in enumerate(embeddings):
                    if len(embedding) != vector_dim:
                        raise ValueError(
                            f"第 {start + idx + 1} 条嵌入维度为 {len(embedding)}，与模型维度 {vector_dim} 不一致。"
                        )

                for item, embedding in zip(batch, embeddings):
                    emb_list = self._to_float_list(embedding)
                    domain, dataset_id, source_id, normalized_meta = resolve_item_identity(item)
                    meta = self._serialize_metadata(normalized_meta)

                    # 计算内容哈希
                    content_hash = self._record_hash(item)
                    doc_meta = item["_document_meta"]
                    header = lexical_text(source_id + " " + doc_meta.get("title", "") + " " + doc_meta.get("section_title", ""))
                    body = lexical_text((item.get("source_content") or "") + " " + (item.get("rewritten_content") or ""))

                    cur.execute(
                        """
                        INSERT INTO searchable_documents
                          (domain, dataset_id, source_table, source_id, content, embedding,
                           company_name, report_year, credit_no, origin_status, metadata, content_hash,
                           source_content, document_meta, search_vector)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                          setweight(to_tsvector('simple', %s), 'A') || setweight(to_tsvector('simple', %s), 'B'))
                        ON CONFLICT (domain, dataset_id, source_id)
                          DO UPDATE SET
                            source_table  = EXCLUDED.source_table,
                            content       = EXCLUDED.content,
                            embedding     = EXCLUDED.embedding,
                            company_name  = EXCLUDED.company_name,
                            report_year   = EXCLUDED.report_year,
                            credit_no     = EXCLUDED.credit_no,
                            origin_status = EXCLUDED.origin_status,
                            metadata      = EXCLUDED.metadata,
                            content_hash  = EXCLUDED.content_hash,
                            source_content = EXCLUDED.source_content,
                            document_meta = EXCLUDED.document_meta,
                            search_vector = EXCLUDED.search_vector,
                            created_at    = CURRENT_TIMESTAMP;
                        """,
                        (
                            domain,
                            dataset_id,
                            item.get("source_table") or dataset_id,
                            source_id,
                            item.get("rewritten_content") or "",
                            emb_list,
                            self._maybe_text(item.get("company_name")),
                            self._maybe_text(item.get("report_year")),
                            self._maybe_text(item.get("credit_no")),
                            self._maybe_text(item.get("origin_status")),
                            meta,
                            content_hash,
                            item.get("source_content"),
                            json.dumps(doc_meta, ensure_ascii=False),
                            header,
                            body,
                        ),
                    )
                total += len(batch)

            # Replace only this logical document/version within its own namespace.
            # Removing obsolete chunks prevents shortened updates from searching old
            # exceptions. Other domains, datasets and versions remain untouched.
            for (domain, dataset_id, parent), source_ids in groups.items():
                cur.execute("""DELETE FROM searchable_documents
                    WHERE domain=%s AND dataset_id=%s
                      AND (document_meta->>'document_source_id'=%s OR source_id=%s)
                      AND NOT (source_id = ANY(%s))""",
                    (domain, dataset_id, parent, parent, source_ids))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        log.info("✓ 嵌入并入库完成，共处理 %s 条", total)
        return total

    def _ensure_table(self, conn, vector_dim: int, log: logging.Logger) -> None:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.searchable_documents');")
        table_exists = cur.fetchone()[0] is not None

        text_cols = [
            "domain",
            "dataset_id",
            "source_table",
            "source_id",
            "content",
            "company_name",
            "report_year",
            "credit_no",
            "origin_status",
            "metadata",
        ]

        if not table_exists:
            cur.execute(
                f"""
                CREATE TABLE searchable_documents (
                    id BIGSERIAL PRIMARY KEY,
                    domain        TEXT NOT NULL DEFAULT 'legacy',
                    dataset_id    TEXT NOT NULL DEFAULT 'legacy',
                    source_table  TEXT,
                    source_id     TEXT,
                    content       TEXT,
                    embedding     vector({vector_dim}),
                    company_name  TEXT,
                    report_year   TEXT,
                    credit_no     TEXT,
                    origin_status TEXT,
                    metadata      TEXT,
                    content_hash  TEXT DEFAULT '',
                    source_content TEXT,
                    document_meta JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    search_vector TSVECTOR,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(domain, dataset_id, source_id)
                );
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sd_domain_dataset_source
                ON searchable_documents (domain, dataset_id, source_id);
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sd_search_vector ON searchable_documents USING GIN (search_vector);")
            conn.commit()
            return

        for col in text_cols:
            cur.execute(f"ALTER TABLE searchable_documents ADD COLUMN IF NOT EXISTS {col} TEXT;")

        cur.execute(
            """
            ALTER TABLE searchable_documents
            ADD COLUMN IF NOT EXISTS content_hash TEXT DEFAULT '';
            """
        )
        cur.execute(
            """
            ALTER TABLE searchable_documents
            ALTER COLUMN content_hash SET DEFAULT '';
            """
        )
        cur.execute(
            """
            ALTER TABLE searchable_documents
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
            """
        )
        cur.execute(f"ALTER TABLE searchable_documents ADD COLUMN IF NOT EXISTS embedding vector({vector_dim});")
        conn.commit()

        cur.execute(
            """
            UPDATE searchable_documents
            SET content_hash = ''
            WHERE content_hash IS NULL;
            """
        )
        conn.commit()

        cur.execute(
            """
            SELECT atttypmod AS dim
            FROM pg_attribute
            WHERE attrelid = 'searchable_documents'::regclass
              AND attname = 'embedding';
            """
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("表存在但未检测到 embedding 列，请检查表结构。")
        table_dim = int(row[0])

        if table_dim != vector_dim:
            log.warning("检测到表向量维度=%s 与 模型维度=%s 不一致，准备处理...", table_dim, vector_dim)
            cur.execute("SELECT COUNT(*) FROM searchable_documents;")
            rowcount = cur.fetchone()[0]
            if rowcount == 0:
                log.info("表为空，将自动调整 embedding 列维度以匹配模型。")
                cur.execute(f"ALTER TABLE searchable_documents ALTER COLUMN embedding TYPE vector({vector_dim});")
                conn.commit()
            else:
                raise RuntimeError(
                    "searchable_documents 已有数据且向量维度与模型不一致，请先迁移或手动调整表结构。"
                )

        conn.commit()

    def _serialize_metadata(self, raw_meta) -> Optional[str]:
        if raw_meta is None:
            return None
        if isinstance(raw_meta, str):
            return raw_meta
        return json.dumps(raw_meta, ensure_ascii=False, default=str)

    def _maybe_text(self, value) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _to_float_list(self, embedding) -> List[float]:
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        if isinstance(embedding, (list, tuple)) and embedding and isinstance(embedding[0], (list, tuple)):
            embedding = [x for row in embedding for x in row]
        return [float(x) for x in embedding]

    def _filter_changed_items(self, items: List[dict], log: logging.Logger) -> List[dict]:
        """
        过滤出内容发生变化的数据项

        Args:
            items: 待处理的数据项列表
            log: 日志对象

        Returns:
            需要更新的数据项列表
        """
        if not items:
            return []

        # 批量查询现有记录的哈希值
        conn = psycopg2.connect(**self.config.db_config)
        cur = conn.cursor()

        try:
            # Startup can legitimately find no table on a new database. Leave
            # creation to the existing writer, rather than failing the first
            # incremental upload before it reaches _ensure_table.
            cur.execute("SELECT to_regclass('public.searchable_documents');")
            if cur.fetchone()[0] is None:
                return items

            # 构建查询条件
            conditions = []
            for item in items:
                domain, dataset_id, source_id, _ = resolve_item_identity(item)
                conditions.append((domain, dataset_id, source_id))

            # 批量查询
            query = """
                SELECT domain, dataset_id, source_id, content_hash
                FROM searchable_documents
                WHERE (domain, dataset_id, source_id) IN %s
            """
            cur.execute(query, (tuple(conditions),))
            existing_hashes = {
                (row[0], row[1], row[2]): row[3] for row in cur.fetchall()
            }

            log.info("从数据库查询到 %s 条现有记录的哈希", len(existing_hashes))

        finally:
            conn.close()

        # 过滤变化的数据
        changed_items = []
        skipped_count = 0

        for item in items:
            domain, dataset_id, source_id, _ = resolve_item_identity(item)
            original_data = item.get("original_data") or {}

            # 计算新数据的哈希
            new_hash = self._record_hash(item)

            # 查找现有哈希
            key = (domain, dataset_id, source_id)
            existing_hash = existing_hashes.get(key)

            if existing_hash is None:
                # 新记录
                changed_items.append(item)
                log.debug("新记录: %s/%s", dataset_id, source_id)
            elif existing_hash != new_hash:
                # 内容变化
                changed_items.append(item)
                log.debug("内容变化: %s/%s (旧哈希: %s, 新哈希: %s)",
                         dataset_id, source_id, existing_hash[:8], new_hash[:8])
            else:
                # 内容未变化，跳过
                skipped_count += 1
                log.debug("跳过未变化: %s/%s/%s", domain, dataset_id, source_id)

        log.info("增量检测结果: 需更新 %s 条, 跳过 %s 条",
                len(changed_items), skipped_count)

        return changed_items

    @staticmethod
    def _record_hash(item: dict) -> str:
        original = item.get("original_data") or {}
        if "_document_meta" not in item:
            return compute_content_hash(original)
        return compute_content_hash({"original_data": original,
                                     "source_content": item.get("source_content"),
                                     "document_meta": item["_document_meta"], "format": 1})
