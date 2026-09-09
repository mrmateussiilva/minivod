from fastapi import APIRouter, Request

from app.routes.bridge import LegacyEndpoint, params_from_post, params_from_request
from app.services.runtime import core

router = APIRouter()

async def player(request: Request):
    params = await params_from_post(request) if request.method == "POST" else params_from_request(request)
    return LegacyEndpoint(request).call(core().Handler.handle_player_api, params)

router.add_api_route("/player_api.php", player, methods=["GET", "POST"])
router.add_api_route("/player_api", player, methods=["GET", "POST"])

@router.get("/get.php")
@router.get("/get")
def get_playlist(request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_get_php, params_from_request(request))

@router.get("/xmltv.php")
@router.get("/xmltv")
def xmltv(request: Request):
    return LegacyEndpoint(request).call(core().Handler.handle_xmltv, params_from_request(request))
