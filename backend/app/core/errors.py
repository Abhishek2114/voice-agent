from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(what: str = "Resource") -> APIError:
    return APIError(404, "not_found", f"{what} not found")


def forbidden(message: str = "You do not have permission to do that") -> APIError:
    return APIError(403, "forbidden", message)


def bad_request(message: str, code: str = "bad_request", **details: Any) -> APIError:
    return APIError(400, code, message, details)


def conflict(message: str, code: str = "conflict") -> APIError:
    return APIError(409, code, message)


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, APIError)
    return JSONResponse(
        status_code=exc.status,
        content={"detail": exc.message, "error": {"code": exc.code, "message": exc.message, **exc.details}},
    )
