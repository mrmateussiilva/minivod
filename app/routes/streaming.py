from fastapi import APIRouter, Request

from app.routes.bridge import LegacyEndpoint
from app.services.runtime import core

router = APIRouter()

@router.get("/vod/{video_id}/index.m3u8")
def playlist(video_id: int, request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_playlist, video_id)

@router.get("/vod/{video_id}/{segment}")
def segment(video_id: int, segment: str, request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_segment, video_id, segment)

@router.get("/vod/{video_id}/status")
def status(video_id: int, request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_status, video_id)

@router.get("/{stream_type:movie|series}/{username}/{password}/{video_id}.m3u8")
@router.get("/{stream_type:movie|series}/{username}/{password}/{video_id}")
def xtream_playlist(stream_type: str, username: str, password: str, video_id: int, request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_xtream_playlist, stream_type, username, password, video_id)

@router.get("/{stream_type:movie|series}/{username}/{password}/{video_id}/{segment}")
def xtream_segment(stream_type: str, username: str, password: str, video_id: int, segment: str, request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_xtream_segment, stream_type, username, password, video_id, segment)
