"""One error shape for the whole API.

Every failure — a missing room, bad input, a broken upload, an LLM that could not
be repaired — comes back in the same JSON shape, so a client never has to guess.
We follow RFC 9457 ("Problem Details for HTTP APIs"):

    {
      "type":     "https://porua.ai/problems/room-not-found",
      "title":    "Room not found",
      "status":   404,
      "detail":   "No room with id 0b0c...",
      "instance": "/rooms/0b0c...",
      "code":     "ROOM_NOT_FOUND"
    }

`code` is our addition: a stable machine-readable string. Clients should switch on
`code`, never on `detail`, because detail is written for humans and may change.
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_BASE_URI = "https://porua.ai/problems"


class AppError(Exception):
    """Base class for every error we raise on purpose.

    Subclasses set a status and a code. Anything that reaches the client should be
    one of these — an unhandled exception means we missed a case.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"
    title: str = "Internal server error"

    def __init__(self, detail: str | None = None, **extra: object) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    title = "Resource not found"


class ValidationFailedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "VALIDATION_FAILED"
    title = "Request could not be processed"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    title = "Conflicting request"


class UnsupportedFileError(AppError):
    """Wrong type, or a type that does not match the contents.

    415 rather than 422: the request itself was well-formed, and the problem is
    the media it carried. A client seeing this should offer a different file, not
    retry the same one.
    """

    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "UNSUPPORTED_TYPE"
    title = "Unsupported file type"


class FileTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "FILE_TOO_LARGE"
    title = "File too large"


class UnreadableFileError(AppError):
    """The right type, but nothing usable came out of it.

    Covers a corrupt archive, a password-protected file, a scanned PDF with no
    text layer, and a file that opens perfectly and contains no words. The
    distinction between them is carried in `code`, set per case at the raise site,
    because "we could not read your file" is the same sentence for all four and
    the reason is the part worth acting on.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "UNREADABLE_FILE"
    title = "File could not be read"

    def __init__(self, detail: str, *, code: str, **extra: object) -> None:
        self.code = code
        super().__init__(detail, **extra)


def _problem(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    **extra: object,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"{PROBLEM_BASE_URI}/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
    }
    body.update({k: v for k, v in extra.items() if v is not None})
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _problem(
            request,
            status_code=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            **exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default 422 body has a different shape from everything else.
        # Rewriting it here keeps one error format across the whole API.
        return _problem(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="VALIDATION_FAILED",
            title="Request could not be processed",
            detail="One or more fields are invalid.",
            errors=[
                {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
                for e in exc.errors()
            ],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _problem(
            request,
            status_code=exc.status_code,
            code="HTTP_ERROR",
            title=str(exc.detail),
            detail=str(exc.detail),
        )
