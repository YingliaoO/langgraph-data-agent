"""Controlled upload storage and bounded CSV/Excel validation."""

import csv
import io
from pathlib import Path
from uuid import UUID
import zipfile

from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core.database import owner_filter
from multisource_data_agent.infrastructure.persistence.db.models.uploaded_file import UploadedFile
from multisource_data_agent.interfaces.http.security import safe_error

ALLOWED_FILE_TYPES = {".csv", ".xlsx", ".xls"}
CHUNK_BYTES = 64 * 1024


def controlled_path(storage_name: str) -> Path:
    root = Path(settings.UPLOAD_DIR).resolve()
    if Path(storage_name).name != storage_name or "\\" in storage_name:
        raise safe_error(400, "invalid_file_id", "Invalid upload identity")
    try:
        UUID(Path(storage_name).stem)
    except ValueError:
        raise safe_error(400, "invalid_file_id", "Invalid upload identity") from None
    path = (root / storage_name).resolve()
    if path.suffix not in ALLOWED_FILE_TYPES or not path.is_relative_to(root):
        raise safe_error(400, "invalid_upload_path", "Invalid upload location")
    return path


def resolve_upload(db, file_id: str, subject: str) -> UploadedFile:
    try:
        file_id = str(UUID(file_id))
    except ValueError:
        raise safe_error(400, "invalid_file_id", "Invalid upload identity") from None
    record: UploadedFile | None = (
        db.query(UploadedFile)
        .filter(UploadedFile.id == file_id, owner_filter(UploadedFile.user_id, subject))
        .first()
    )
    if record is None or not controlled_path(record.storage_name).is_file():
        raise safe_error(404, "file_not_found", "Uploaded file not found")
    return record


def validate_dataset_bytes(content: bytes, suffix: str) -> None:
    """Reject binary files disguised as CSV and invalid Excel containers."""
    valid = bool(content)
    if suffix == ".csv":
        try:
            text = content.decode("utf-8-sig")
            valid = valid and "\0" not in text and bool(next(csv.reader(io.StringIO(text)), []))
        except (UnicodeError, csv.Error):
            valid = False
    elif suffix == ".xlsx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                valid = valid and {"[Content_Types].xml", "xl/workbook.xml"} <= names
                valid = (
                    valid
                    and sum(info.file_size for info in archive.infolist())
                    <= settings.FILE_UPLOAD_MAX_MB * 1024 * 1024 * 20
                )
        except zipfile.BadZipFile:
            valid = False
    elif suffix == ".xls":
        valid = content.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    else:
        valid = False
    if not valid:
        raise safe_error(
            400, "invalid_dataset", "The file does not contain a supported CSV/Excel dataset"
        )
