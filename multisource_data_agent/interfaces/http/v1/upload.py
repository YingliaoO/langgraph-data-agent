"""Authenticated, bounded dataset upload API."""
from pathlib import Path
from uuid import uuid4

import aiofiles
from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core.database import get_db
from multisource_data_agent.infrastructure.core.uploads import (
    CHUNK_BYTES, controlled_path, resolve_upload, validate_dataset_bytes,
)
from multisource_data_agent.infrastructure.persistence.db.models.uploaded_file import UploadedFile
from multisource_data_agent.interfaces.http.security import require_user, safe_error

router = APIRouter()
ALLOWED_FILE_TYPES = {'.csv', '.xlsx', '.xls'}


@router.post("/file")
async def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db), subject: str = Depends(require_user)):
    supplied_name = file.filename or ""
    if not supplied_name or Path(supplied_name).name != supplied_name or "\\" in supplied_name or len(supplied_name) > 255:
        raise safe_error(400, "invalid_filename", "Invalid filename")
    suffix = Path(supplied_name).suffix.lower()
    if suffix not in ALLOWED_FILE_TYPES:
        raise safe_error(400, "unsupported_file_type", "Only CSV, XLSX and XLS are accepted")
    file_id = str(uuid4())
    storage_name = file_id + suffix
    path = controlled_path(storage_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    limit = settings.FILE_UPLOAD_MAX_MB * 1024 * 1024
    try:
        async with aiofiles.open(path, "xb") as destination:
            while chunk := await file.read(CHUNK_BYTES):
                size += len(chunk)
                if size > limit:
                    raise safe_error(413, "upload_too_large", "The upload exceeds the configured size limit")
                await destination.write(chunk)
        validate_dataset_bytes(path.read_bytes(), suffix)
        record = UploadedFile(id=file_id, user_id=subject, storage_name=storage_name, original_name=supplied_name, size_bytes=size)
        db.add(record)
        db.commit()
    except BaseException:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return JSONResponse({
        "success": True, "file_id": file_id, "filename": storage_name,
        "original_name": supplied_name, "size": size, "file_type": suffix,
        "file_path": storage_name,  # Deprecated display field, never accepted as a server path.
        "file_url": f"/api/v1/upload/files/{storage_name}",
    })


@router.get("/files/{filename}")
async def get_uploaded_file(filename: str, db: Session = Depends(get_db), subject: str = Depends(require_user)):
    path = controlled_path(filename)
    record = resolve_upload(db, path.stem, subject)
    if record.storage_name != filename:
        raise safe_error(404, "file_not_found", "Uploaded file not found")
    return FileResponse(path, filename=record.original_name)


@router.delete("/{file_id}")
async def delete_uploaded_file(file_id: str, db: Session = Depends(get_db), subject: str = Depends(require_user)):
    record = resolve_upload(db, file_id, subject)
    controlled_path(record.storage_name).unlink()
    db.delete(record)
    db.commit()
    return {"success": True, "message": "File deleted"}
