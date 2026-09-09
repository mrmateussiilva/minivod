from __future__ import annotations

import json
from types import MethodType
from pathlib import Path
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.services.runtime import core


class LegacyEndpoint:
    """Adapts legacy business handlers to Starlette responses, not sockets."""

    def __init__(self, request: Request):
        self.request = request
        self.response: Response | None = None
        self.form_data: dict[str, list[str]] = {}

    def __getattr__(self, name: str) -> Any:
        method = getattr(core().Handler, name, None)
        if callable(method):
            return MethodType(method, self)
        raise AttributeError(name)

    def base_url(self) -> str:
        configured = core().CONFIG.base_url
        if configured:
            return configured.rstrip("/")
        return str(self.request.base_url).rstrip("/")

    def send_json(self, status: int, payload: object) -> None:
        self.response = JSONResponse(payload, status_code=status)

    def send_text(self, status: int, text: str, content_type: str) -> None:
        self.response = Response(
            text.encode("utf-8"), status_code=status,
            headers={"Content-Type": content_type},
        )

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_json(404, {"error": "arquivo não encontrado"})
            return
        self.response = FileResponse(path, media_type=content_type)

    def read_form_body(self) -> dict[str, list[str]]:
        return self.form_data

    def send_redirect(self, location: str, status_code: int = 303) -> None:
        from fastapi.responses import RedirectResponse
        self.response = RedirectResponse(location, status_code=status_code)

    def redirect_admin(self, location: str = "/admin") -> None:
        self.send_redirect(location, status_code=303)

    def call(self, handler: Callable[..., None], *args: object) -> Response:
        handler(self, *args)
        return self.response or Response(status_code=204)


def params_from_request(request: Request) -> dict[str, list[str]]:
    return {key: request.query_params.getlist(key) for key in request.query_params}


async def params_from_post(request: Request) -> dict[str, list[str]]:
    params = params_from_request(request)
    form = await request.form()
    for key in form:
        params[key] = [str(value) for value in form.getlist(key)]
    return params


def json_response(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(content=json.loads(json.dumps(payload)), status_code=status)
