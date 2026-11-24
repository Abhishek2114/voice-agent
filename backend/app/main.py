"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agents.router import router as agents_router
from app.ai.hub import get_hub
from app.analytics.router import router as analytics_router
from app.audio.router import router as voice_router
from app.audio.sessions import manager
from app.auth.router import router as auth_router
from app.calls.router import router as calls_router
from app.campaigns.router import router as campaigns_router
from app.compliance.router import router as compliance_router
from app.contacts.router import router as contacts_router
from app.core.config import get_settings
from app.core.errors import APIError, api_error_handler
from app.core.logging import configure_logging
from app.database.migrate import upgrade_database
from app.database.session import dispose_engine, sessionmaker
from app.imports.router import router as imports_router
from app.knowledge.router import router as knowledge_router
from app.system.router import router as system_router
from app.telephony.router import router as telephony_router
from app.users.router import router as users_router
from app.workspaces.router import router as workspaces_router

log = logging.getLogger(__name__)
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
MAX_BODY = 64 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    settings.resolved_secret()
    await upgrade_database()
    async with sessionmaker()() as db:
        await get_hub().load(db)
    log.info("voice-agent API ready on %s:%s", settings.host, settings.port)
    yield
    await manager.shutdown()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    if not settings.is_test:
        configure_logging()
    app = FastAPI(title="voice-agent API", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs" if settings.env != "production" else None,
                  openapi_url="/api/openapi.json" if settings.env != "production" else None, redoc_url=None)  # fmt: skip

    app.add_exception_handler(APIError, api_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": [str(x) for x in e.get("loc", [])], "msg": e.get("msg", "")} for e in exc.errors()][:20]
        first = errors[0] if errors else {"loc": [], "msg": "invalid request"}
        msg = f"{'.'.join(first['loc'][1:]) or 'request'}: {first['msg']}"
        return JSONResponse(status_code=422, content={"detail": msg, "error": {"code": "validation_error",
                                                                                "message": msg, "errors": errors}})  # fmt: skip

    @app.middleware("http")
    async def security(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # cross-origin state changes are refused outright (defence in depth on top of CSRF tokens)
        if request.method in UNSAFE:
            origin = request.headers.get("origin")
            if origin and origin != "null":
                host = request.headers.get("host", "")
                o = urlparse(origin)
                if origin not in settings.cors_origins and o.netloc != host:
                    return JSONResponse(status_code=403, content={"detail": "Cross-origin request refused",
                                                                  "error": {"code": "bad_origin"}})  # fmt: skip
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_BODY:
                return JSONResponse(status_code=413, content={"detail": "Request too large"})
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if not request.url.path.startswith("/api/docs"):
            h.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if request.url.path.startswith("/api/") and "Cache-Control" not in h:
            h["Cache-Control"] = "no-store"
        if settings.cookie_secure:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["content-type", "x-csrf-token"])  # fmt: skip

    for r in (auth_router, users_router, workspaces_router, agents_router, contacts_router, imports_router,
              compliance_router, knowledge_router, campaigns_router, calls_router, voice_router, analytics_router,
              telephony_router, system_router):  # fmt: skip
        app.include_router(r)
    return app


app = create_app()
