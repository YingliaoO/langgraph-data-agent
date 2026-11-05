
import logging
import tempfile
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, UploadFile, File, Form
from openai import APIConnectionError, AuthenticationError, OpenAIError, RateLimitError

from kb_service.api.deps import get_config
from kb_service.core.config import Config, clone_config
from kb_service.schemas.ingest import ExcelIngestRequest, MySQLIngestRequest
from kb_service.schemas.search import SearchRequest, HybridSearchRequest
from kb_service.services.processor import DataProcessor
from kb_service.services.document_identity import (
    DEFAULT_ENTERPRISE_DOMAIN,
    stable_upload_dataset_id,
)
from kb_service.services.search import VectorSearcher
from kb_service.services.mysql_ingest import MySQLIngestor

logger = logging.getLogger(__name__)

router = APIRouter()


DATASET_UPLOAD_SUFFIXES = {".csv", ".xlsx", ".xls"}


@router.post(
    "/ingest/dataset/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a CSV/Excel dataset and push embeddings into pgvector",
)
async def upload_and_ingest_dataset(
    background: BackgroundTasks,
    file: UploadFile = File(..., description="CSV/XLSX/XLS dataset to upload"),
    incremental: bool = Form(False, description="Whether to use incremental mode"),
    domain: str = Form(DEFAULT_ENTERPRISE_DOMAIN, description="Stable knowledge domain."),
    dataset_id: Optional[str] = Form(None, description="Stable dataset identity."),
    config: Config = Depends(get_config),
):
    """Canonical tabular dataset ingestion endpoint used by the Data Agent."""

    filename = Path(file.filename or "dataset").name
    suffix = Path(filename).suffix.lower()
    if suffix not in DATASET_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只支持 CSV、XLSX、XLS 数据集",
        )

    temp_dir = tempfile.mkdtemp()
    temp_file_path = Path(temp_dir) / filename

    try:
        content = await file.read()
        with open(temp_file_path, "wb") as buffer:
            buffer.write(content)

        size_bytes = temp_file_path.stat().st_size
        effective_dataset_id = dataset_id or stable_upload_dataset_id(content, filename=filename)
        logger.info("Dataset uploaded to temporary path: %s", temp_file_path)

        def run_ingest() -> None:
            try:
                local_config = clone_config(config)
                processor = DataProcessor(
                    local_config,
                    incremental=incremental,
                    domain=domain,
                    dataset_id=effective_dataset_id,
                )
                if suffix == ".csv":
                    processor.process_csv(str(temp_file_path))
                else:
                    local_config.excel_file_path = str(temp_file_path)
                    processor.process_excel()
                logger.info("Dataset ingestion completed: %s", temp_file_path)
            except Exception:
                logger.exception("Dataset ingestion failed for %s", temp_file_path)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        background.add_task(run_ingest)
        return {
            "message": "dataset ingest started",
            "filename": filename,
            "dataset_type": "csv" if suffix == ".csv" else "excel",
            "mode": "incremental" if incremental else "full",
            "size_bytes": size_bytes,
            "backend": "pgvector",
            "domain": domain,
            "dataset_id": effective_dataset_id,
        }
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("Dataset upload failed")
        raise HTTPException(status_code=500, detail=f"数据集上传失败: {str(exc)}")


@router.post(
    "/ingest/excel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Process an Excel file and push embeddings into pgvector",
)
def ingest_excel(payload: ExcelIngestRequest, background: BackgroundTasks, config: Config = Depends(get_config)):
    excel_path = Path(payload.excel_path)
    if not excel_path.exists():
        raise HTTPException(status_code=404, detail=f"Excel file not found: {excel_path}")

    def run_ingest() -> None:
        try:
            local_config = clone_config(config)
            local_config.excel_file_path = str(excel_path)
            processor = DataProcessor(local_config, incremental=payload.incremental)
            processor.process_excel()
        except Exception:  # pragma: no cover - background task logging
            logger.exception("Excel ingest failed for %s", excel_path)

    background.add_task(run_ingest)
    mode = "增量" if payload.incremental else "全量"
    logger.info("Excel ingest queued for %s (模式: %s)", excel_path, mode)
    return {
        "message": "ingest started",
        "path": str(excel_path),
        "mode": "incremental" if payload.incremental else "full"
    }


@router.post(
    "/ingest/excel/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload an Excel file and push embeddings into pgvector",
)
async def upload_and_ingest_excel(
    file: UploadFile = File(..., description="Excel file to upload"),
    incremental: bool = Form(False, description="Whether to use incremental mode"),
    background: BackgroundTasks = None,
    config: Config = Depends(get_config)
):
    """
    上传 Excel 文件并处理：
    - 支持主机任意路径的文件上传
    - 文件会保存到临时目录
    - 处理完成后自动清理临时文件
    """
    # 验证文件类型
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(
            status_code=400,
            detail="只支持 Excel 文件 (.xlsx, .xls)"
        )

    # 创建临时文件
    temp_dir = tempfile.mkdtemp()
    temp_file_path = Path(temp_dir) / file.filename

    try:
        # 保存上传的文件
        with open(temp_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        logger.info("文件已上传到临时目录: %s", temp_file_path)

        # 定义后台任务
        def run_ingest() -> None:
            try:
                local_config = clone_config(config)
                local_config.excel_file_path = str(temp_file_path)
                processor = DataProcessor(local_config, incremental=incremental)
                processor.process_excel()
                logger.info("Excel 处理完成，文件: %s", temp_file_path)
            except Exception:
                logger.exception("Excel ingest failed for %s", temp_file_path)
            finally:
                # 清理临时文件
                try:
                    shutil.rmtree(temp_dir)
                    logger.info("临时文件已清理: %s", temp_dir)
                except Exception:
                    logger.exception("清理临时文件失败: %s", temp_dir)

        background.add_task(run_ingest)
        mode = "增量" if incremental else "全量"
        logger.info("Excel 上传处理已加入队列: %s (模式: %s)", file.filename, mode)

        return {
            "message": "文件上传成功，处理已开始",
            "filename": file.filename,
            "mode": "incremental" if incremental else "full",
            "size_bytes": temp_file_path.stat().st_size
        }

    except Exception as e:
        # 如果出错，清理临时文件
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("文件上传失败")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")


@router.post(
    "/ingest/mysql",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fetch rows from MySQL, rewrite/flatten, and store embeddings",
)
def ingest_mysql(
    payload: MySQLIngestRequest,
    background: BackgroundTasks,
    config: Config = Depends(get_config),
):
    payload_data = payload.model_dump()

    def run_ingest() -> None:
        try:
            local_config = clone_config(config)
            ingestor = MySQLIngestor(local_config)
            ingestor.ingest(MySQLIngestRequest(**payload_data))
        except Exception:  # pragma: no cover - background task logging
            logger.exception("MySQL ingest failed for %s", payload_data.get('table'))

    background.add_task(run_ingest)
    logger.info("MySQL ingest queued for %s", payload.table)
    return {"message": "ingest started", "table": payload.table}


@router.post(
    "/search",
    status_code=status.HTTP_200_OK,
    summary="Query pgvector for similar records",
)
def vector_search(payload: SearchRequest, config: Config = Depends(get_config)):
    searcher = VectorSearcher(config)
    try:
        diagnostics = {}
        results = searcher.search_similar(
            query=payload.query,
            top_k=payload.top_k,
            threshold=payload.threshold,
            metric=payload.metric,
            company_filter=payload.company_filter,
            source_tables=payload.source_tables,
            domain=payload.domain,
            dataset_id=payload.dataset_id,
            candidate_k=payload.candidate_k,
            rerank=payload.rerank,
            diagnostics=diagnostics,
            retrieval_mode=payload.retrieval_mode,
            as_of=payload.as_of,
        )
        return {"query": payload.query, "results": results, "count": len(results), "retrieval": diagnostics}
    except AuthenticationError:
        # Treat as service configuration issue (upstream embedding provider auth).
        # Do not leak any credential details into API responses.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding provider authentication failed. Check KB_EMBEDDING_API_KEY/KB_EMBEDDING_BASE_URL.",
        )
    except (RateLimitError, APIConnectionError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding provider unavailable (rate limited or connection error).",
        )
    except OpenAIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding provider returned an error.",
        )


@router.post(
    "/search/hybrid",
    status_code=status.HTTP_200_OK,
    summary="Hybrid search with vector retrieval + reranking",
)
def hybrid_search(payload: HybridSearchRequest, config: Config = Depends(get_config)):
    searcher = VectorSearcher(config)
    try:
        return searcher.hybrid_search(
            query=payload.query,
            vector_top_k=payload.vector_top_k,
            rerank_top_k=payload.rerank_top_k,
            threshold=payload.threshold,
            metric=payload.metric,
            source_tables=payload.source_tables,
            domain=payload.domain,
            dataset_id=payload.dataset_id,
        )
    except AuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding provider authentication failed. Check KB_EMBEDDING_API_KEY/KB_EMBEDDING_BASE_URL.",
        )
    except (RateLimitError, APIConnectionError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding provider unavailable (rate limited or connection error).",
        )
    except OpenAIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding provider returned an error.",
        )
