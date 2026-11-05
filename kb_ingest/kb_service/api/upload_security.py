"""Bounded service uploads and authentication; independent of the Agent package."""

import csv
import hmac
import io
import os
from pathlib import Path
import zipfile
from uuid import uuid4

from fastapi import Header, HTTPException, UploadFile
from starlette.responses import JSONResponse


def require_service_key(x_knowledge_service_key: str = Header("")) -> None:
    expected = os.getenv("KNOWLEDGE_SERVICE_TOKEN", "")
    if len(expected.encode()) < 32:
        raise HTTPException(503, "Knowledge service authentication is not configured")
    if not hmac.compare_digest(x_knowledge_service_key.encode(), expected.encode()):
        raise HTTPException(401, "Service authentication required")


async def receive_dataset(file: UploadFile, path: Path) -> bytes:
    limit = int(os.getenv("FILE_UPLOAD_MAX_MB", "2")) * 1024 * 1024
    size = 0
    try:
        with path.open("xb") as destination:
            while chunk := await file.read(64 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "Upload exceeds the configured size limit")
                destination.write(chunk)
        content = path.read_bytes()  # Already bounded; required by stable dataset hashing.
        if not content:
            raise ValueError("Empty file")
        if path.suffix == ".csv":
            text = content.decode("utf-8-sig")
            if "\0" in text or not next(csv.reader(io.StringIO(text)), []):
                raise ValueError("Invalid CSV")
        elif path.suffix == ".xlsx":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if not {"[Content_Types].xml", "xl/workbook.xml"} <= set(archive.namelist()):
                    raise ValueError("Invalid workbook")
                if sum(i.file_size for i in archive.infolist()) > limit * 20:
                    raise ValueError("Workbook expansion exceeds limit")
        elif path.suffix == ".xls":
            if not content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
                raise ValueError("Invalid legacy workbook")
        else:
            raise ValueError("Unsupported dataset")
        return content
    except (UnicodeError, ValueError, csv.Error, zipfile.BadZipFile):
        raise HTTPException(400, "Invalid CSV/Excel dataset") from None
    finally:
        await file.close()


class RequestBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = int(os.getenv("FILE_UPLOAD_MAX_MB", "2")) * 1024 * 1024 + 64 * 1024
        try:
            declared = int(dict(scope.get("headers", [])).get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse(
                {
                    "detail": {
                        "code": "invalid_content_length",
                        "category": "validation",
                        "message": "Invalid content length",
                        "trace_id": str(uuid4()),
                    }
                },
                status_code=400,
            )(scope, receive, send)
        if declared < 0 or declared > limit:
            return await JSONResponse(
                {
                    "detail": {
                        "code": "upload_too_large",
                        "category": "validation",
                        "message": "Request body exceeds upload limit",
                        "trace_id": str(uuid4()),
                    }
                },
                status_code=413,
            )(scope, receive, send)
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, "Request body exceeds upload limit")
            return message

        return await self.app(scope, bounded_receive, send)
