from pathlib import Path

import pytest


def _read_source_or_skip(path: str) -> str:
    """Read a project source file when that service source is mounted.

    The backend test container intentionally does not contain the separate
    ``kb_ingest`` service source tree.  Service-local source assertions are
    therefore skipped there and are verified separately inside the kb_ingest
    container / end-to-end ingestion smoke.
    """
    source_path = Path(path)
    if not source_path.exists():
        pytest.skip(f"service source is not mounted in this test runtime: {path}")
    return source_path.read_text(encoding="utf-8")


def test_upload_api_only_exposes_tabular_dataset_types() -> None:
    source = Path("multisource_data_agent/interfaces/http/v1/upload.py").read_text(encoding="utf-8")

    assert "ALLOWED_FILE_TYPES = {'.csv', '.xlsx', '.xls'}" in source
    for legacy_suffix in (".txt", ".md", ".json", ".log", ".pdf", ".doc", ".docx"):
        assert legacy_suffix not in source.split("ALLOWED_FILE_TYPES", 1)[1].split("ALLOWED_IMAGE_TYPES", 1)[0]


def test_file_query_is_dataset_ingestion_not_milvus_document_qa() -> None:
    source = Path("multisource_data_agent/application/agents/lg_builder.py").read_text(encoding="utf-8")
    body = source.split("async def create_file_query(", 1)[1].split("async def create_research_plan(", 1)[0]

    assert "KnowledgeService(" not in body
    assert "create_knowledge_query_node" not in body
    assert "/api/v1/knowledge/ingest/dataset/upload" in body
    assert '{".csv", ".xlsx", ".xls"}' in body
    for legacy_suffix in (".txt", ".md", ".json", ".log", ".pdf", ".doc", ".docx"):
        assert legacy_suffix not in body


def test_kb_ingest_has_canonical_dataset_upload_endpoint() -> None:
    source = _read_source_or_skip("kb_ingest/kb_service/api/routes.py")

    assert '"/ingest/dataset/upload"' in source
    assert 'DATASET_UPLOAD_SUFFIXES = {".csv", ".xlsx", ".xls"}' in source
    assert "processor.process_csv(str(temp_file_path))" in source
    assert "processor.process_excel()" in source
    assert '"backend": "pgvector"' in source


def test_data_processor_supports_csv_through_same_pgvector_pipeline() -> None:
    source = _read_source_or_skip("kb_ingest/kb_service/services/processor.py")

    assert "def process_csv(self, csv_path: str) -> None:" in source
    assert "self._process_dataframe(self.dataset_id or path.stem or \"csv_dataset\", df)" in source
    assert '"domain"' in source
    assert '"dataset_id"' in source
    assert "self._finalize_dataset_processing()" in source
    assert "self._embed_and_store(incremental=self.incremental)" in source


def test_router_prompt_describes_file_query_as_dataset_ingestion() -> None:
    source = Path("multisource_data_agent/application/agents/lg_prompts.py").read_text(encoding="utf-8")

    assert "## `file-query` - 数据集导入" in source
    assert "CSV/XLSX/XLS" in source
    assert "通用文档问答不属于核心能力" in source


def test_route_info_no_longer_advertises_generic_document_processing() -> None:
    source = Path("multisource_data_agent/interfaces/http/v1/chat.py").read_text(encoding="utf-8")
    block = source.split('"file-query": {', 1)[1].split("}", 1)[0]

    assert '"name": "数据集导入"' in block
    assert "CSV/XLSX/XLS" in block
    assert "菜谱文档" not in block
