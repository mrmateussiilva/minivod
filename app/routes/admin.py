from __future__ import annotations

import base64
import binascii
import hmac
import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request, status, Header
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.cover_support import image_content_type, is_image_file, is_within
from app.services import admin_service
from app.services.runtime import core

router = APIRouter(prefix="/admin")

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def require_admin(request: Request) -> None:
    config = core().CONFIG
    configured_user = config.admin_user
    configured_password = config.admin_password

    if not configured_user or not configured_password:
        raise HTTPException(status_code=404, detail="rota não encontrada")

    authorization = request.headers.get("Authorization", "")
    try:
        scheme, encoded = authorization.split(" ", 1)
        user, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MiniVOD Admin"'},
        )

    if scheme != "Basic" or not (
        hmac.compare_digest(user, configured_user)
        and hmac.compare_digest(password, configured_password)
    ):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MiniVOD Admin"'},
        )


@router.get("")
def dashboard(
    request: Request,
    success: str | None = None,
    error: str | None = None,
):
    require_admin(request)
    config = core().CONFIG
    stats = admin_service.get_dashboard_stats(config.db, config.cache)
    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "stats": stats,
            "active_page": "dashboard",
            "success": success,
            "error": error,
        },
    )


@router.get("/collections")
def collections(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    hx_request: str | None = Header(default=None),
):
    require_admin(request)
    config = core().CONFIG
    items = admin_service.list_admin_collections(config.db)
    context = {
        "collections": items,
        "active_page": "collections",
        "success": success,
        "error": error,
    }
    if hx_request:
        return templates.TemplateResponse(
            request=request,
            name="admin/partials/_collections_table.html",
            context=context,
        )
    return templates.TemplateResponse(
        request=request,
        name="admin/collections.html",
        context=context,
    )


@router.get("/collections/{collection_id}")
def collection_detail(
    collection_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    success: str | None = None,
    error: str | None = None,
):
    require_admin(request)
    config = core().CONFIG
    details = admin_service.get_collection_details(config.db, collection_id, page=page, per_page=per_page)
    if details is None:
        raise HTTPException(status_code=404, detail="Coleção não encontrada")

    return templates.TemplateResponse(
        request=request,
        name="admin/collection_detail.html",
        context={
            "collection": details,
            "active_page": "collections",
            "success": success,
            "error": error,
        },
    )


@router.get("/collections/{collection_id}/covers")
def covers(
    collection_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=60),
    success: str | None = None,
    error: str | None = None,
):
    require_admin(request)
    config = core().CONFIG
    result = admin_service.get_collection_cover_candidates(config.db, collection_id, page=page, per_page=per_page)
    if result is None:
        raise HTTPException(status_code=404, detail="Coleção não encontrada")

    candidates, total, total_pages, col_name = result
    return templates.TemplateResponse(
        request=request,
        name="admin/covers.html",
        context={
            "collection_id": collection_id,
            "collection_name": col_name,
            "candidates": candidates,
            "total_candidates": total,
            "total_pages": total_pages,
            "page": page,
            "per_page": per_page,
            "active_page": "collections",
            "success": success,
            "error": error,
        },
    )


@router.get("/collections/{collection_id}/cover-preview")
def cover_preview(
    collection_id: int,
    request: Request,
    path: str = Query(""),
):
    require_admin(request)
    config = core().CONFIG
    record = admin_service.get_collection_cover_record(config.db, collection_id)
    if record is None or not record["root"]:
        raise HTTPException(status_code=404, detail="Coleção não encontrada")

    root = record["root"]
    candidate = root / path
    if not is_within(candidate, root) or not is_image_file(candidate) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Imagem não encontrada")

    content_type = image_content_type(candidate) or "image/jpeg"
    return FileResponse(candidate, media_type=content_type)


@router.get("/videos")
def videos(
    request: Request,
    q: str = Query(""),
    collection: int | None = Query(None),
    status_filter: str = Query("active", alias="status"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    success: str | None = None,
    error: str | None = None,
    hx_request: str | None = Header(default=None),
):
    require_admin(request)
    config = core().CONFIG
    items, total, total_pages, collections_list = admin_service.list_admin_videos(
        config.db,
        q=q,
        collection_id=collection,
        status=status_filter,
        page=page,
        per_page=per_page,
    )
    context = {
        "videos": items,
        "total_videos": total,
        "total_pages": total_pages,
        "collections": collections_list,
        "q": q,
        "selected_collection": collection,
        "status": status_filter,
        "page": page,
        "per_page": per_page,
        "active_page": "videos",
        "success": success,
        "error": error,
    }
    
    if hx_request:
        return templates.TemplateResponse(
            request=request,
            name="admin/partials/_videos_table.html",
            context=context,
        )
        
    return templates.TemplateResponse(
        request=request,
        name="admin/videos.html",
        context=context,
    )


@router.get("/videos/{video_id}")
def video_detail(
    video_id: int,
    request: Request,
    success: str | None = None,
    error: str | None = None,
):
    require_admin(request)
    config = core().CONFIG
    video = admin_service.get_video_details(config.db, config.cache, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    return templates.TemplateResponse(
        request=request,
        name="admin/video_detail.html",
        context={
            "video": video,
            "active_page": "videos",
            "success": success,
            "error": error,
        },
    )


@router.get("/users")
def users(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    hx_request: str | None = Header(default=None),
):
    require_admin(request)
    config = core().CONFIG
    items = admin_service.list_admin_users(config.db)
    context = {
        "users": items,
        "active_page": "users",
        "success": success,
        "error": error,
    }
    if hx_request:
        return templates.TemplateResponse(
            request=request,
            name="admin/partials/_users_table.html",
            context=context,
        )
    return templates.TemplateResponse(
        request=request,
        name="admin/users.html",
        context=context,
    )


# ---------------------------------------------------------------------
# POST Handlers
# ---------------------------------------------------------------------


@router.post("/users")
async def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    max_connections: int = Form(1),
    expires_days: int | None = Form(None),
):
    require_admin(request)
    config = core().CONFIG
    try:
        admin_service.create_admin_user(
            config.db,
            username=username.strip(),
            password=password,
            max_connections=max_connections,
            expires_days=expires_days,
        )
        return RedirectResponse(
            url=f"/admin/users?success={quote(f'Usuário {username} criado com sucesso!')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except sqlite3.IntegrityError:
        return RedirectResponse(
            url=f"/admin/users?error={quote('Este nome de usuário já existe.')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/admin/users?error={quote(str(e))}",
            status_code=status.HTTP_303_SEE_OTHER,
        )


@router.post("/users/{user_id}/password")
async def change_password(
    user_id: int,
    request: Request,
    password: str = Form(...),
):
    require_admin(request)
    config = core().CONFIG
    try:
        updated = admin_service.set_user_password(config.db, user_id, password)
        if not updated:
            return RedirectResponse(
                url=f"/admin/users?error={quote('Usuário não encontrado.')}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        return RedirectResponse(
            url=f"/admin/users?success={quote('Senha atualizada com sucesso!')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/admin/users?error={quote(str(e))}",
            status_code=status.HTTP_303_SEE_OTHER,
        )


@router.post("/users/{user_id}/{action}")
async def toggle_user(
    user_id: int,
    action: str,
    request: Request,
    hx_request: str | None = Header(default=None),
):
    require_admin(request)
    if action not in ("enable", "disable"):
        raise HTTPException(status_code=400, detail="Ação inválida")
    config = core().CONFIG
    enable = action == "enable"
    updated = admin_service.set_user_status(config.db, user_id, enable)
    if not updated:
        if hx_request:
            items = admin_service.list_admin_users(config.db)
            return templates.TemplateResponse(
                request=request,
                name="admin/partials/_users_table.html",
                context={"users": items, "error": "Usuário não encontrado."},
            )
        return RedirectResponse(
            url=f"/admin/users?error={quote('Usuário não encontrado.')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    msg = "Usuário habilitado com sucesso!" if enable else "Usuário desativado."
    if hx_request:
        items = admin_service.list_admin_users(config.db)
        return templates.TemplateResponse(
            request=request,
            name="admin/partials/_users_table.html",
            context={"users": items, "success": msg},
        )
    return RedirectResponse(
        url=f"/admin/users?success={quote(msg)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/collections/{collection_id}/cover")
async def set_cover(
    collection_id: int,
    request: Request,
    cover: str = Form(...),
):
    require_admin(request)
    config = core().CONFIG
    ok = admin_service.set_collection_cover(config.db, collection_id, cover)
    if not ok:
        return RedirectResponse(
            url=f"/admin/collections/{collection_id}?error={quote('Imagem de capa inválida ou inacessível.')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        url=f"/admin/collections/{collection_id}?success={quote('Capa atualizada com sucesso!')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/collections/{collection_id}/cover-auto")
@router.post("/collections/{collection_id}/cover/auto")
async def auto_cover(
    collection_id: int,
    request: Request,
):
    require_admin(request)
    config = core().CONFIG
    ok = admin_service.auto_select_collection_cover(config.db, collection_id)
    if not ok:
        return RedirectResponse(
            url=f"/admin/collections/{collection_id}?error={quote('Não foi possível selecionar uma capa automaticamente.')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        url=f"/admin/collections/{collection_id}?success={quote('Seleção automática de capa realizada!')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/collections/{collection_id}/cover-remove")
async def remove_cover(
    collection_id: int,
    request: Request,
):
    require_admin(request)
    config = core().CONFIG
    ok = admin_service.remove_collection_cover(config.db, collection_id)
    if not ok:
        return RedirectResponse(
            url=f"/admin/collections/{collection_id}?error={quote('Coleção não encontrada.')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        url=f"/admin/collections/{collection_id}?success={quote('Capa desvinculada com sucesso.')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
