from fastapi import APIRouter, Request

from app.routes.bridge import LegacyEndpoint
from app.services.runtime import core

router = APIRouter()

@router.get("/covers/{collection_id}")
def cover(collection_id: int, request: Request):
    response = LegacyEndpoint(request).call(core().Handler.handle_collection_cover, collection_id)
    response.headers["Cache-Control"] = "public, max-age=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
