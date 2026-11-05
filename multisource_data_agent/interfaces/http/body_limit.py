"""Bound the raw request stream as well as each uploaded file."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from multisource_data_agent.interfaces.http.security import request_trace_id
from uuid import uuid4


class RequestBodyLimit:
    def __init__(self, app, limit_bytes):
        self.app = app
        self.limit_bytes = limit_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = int(self.limit_bytes()) + 64 * 1024  # Bounded multipart framing overhead.
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse(
                {
                    "detail": {
                        "code": "invalid_content_length",
                        "category": "validation",
                        "message": "Invalid content length",
                        "trace_id": request_trace_id.get() or str(uuid4()),
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
                        "trace_id": request_trace_id.get() or str(uuid4()),
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
