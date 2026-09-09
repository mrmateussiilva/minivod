from fastapi import APIRouter, Request

from app.routes.bridge import LegacyEndpoint
from app.services.runtime import core

router = APIRouter()

@router.get("/")
def root(request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_root)

@router.get("/health")
def health(request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_health)

@router.get("/compat")
def compat(request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_compat)

@router.get("/collections")
def collections(request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_collections)

@router.get("/collections/{collection_id}/videos")
def collection_videos(collection_id: int, request: Request):
    from app.routes.bridge import params_from_request
    return LegacyEndpoint(request).call(core().Handler.handle_collection_videos, collection_id, params_from_request(request))

@router.get("/videos/{video_id}")
def video(video_id: int, request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_video, video_id)
