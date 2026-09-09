from __future__ import annotations

import logging
from pathlib import Path
import re

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.config import settings_from_env
from app.routes import admin, covers, streaming, system, xtream
from app.services.runtime import configure

settings = settings_from_env()
configure(settings)

app = FastAPI(title="MiniVOD", docs_url=None, redoc_url=None)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")
app.include_router(system.router)
app.include_router(xtream.router)
app.include_router(streaming.router)
app.include_router(covers.router)
app.include_router(admin.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    if request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
    return response


_PASSWORD_QUERY = re.compile(r"([?&]password=)[^&\s]+", re.IGNORECASE)
_STREAM_PASSWORD = re.compile(r"/(movie|series)/([^/]+)/([^/]+)/", re.IGNORECASE)


@app.middleware("http")
async def request_log(request: Request, call_next):
    response = await call_next(request)
    path = _PASSWORD_QUERY.sub(r"\1***", str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
    path = _STREAM_PASSWORD.sub(r"/\1/\2/***/", path)
    logging.getLogger("minivod.http").info("%s %s %s", request.method, path, response.status_code)
    return response
