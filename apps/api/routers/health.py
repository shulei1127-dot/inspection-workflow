"""Health check endpoint."""

from fastapi import APIRouter

from services import dingtalk_client, pts_client, yunji_client

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health_check():
    """Health check: verify PTS API + dws CLI + yunji session connectivity."""
    pts_ok = False
    dws_ok = False
    yunji_result = {"valid": False, "reason": "Not checked"}
    pts_error = None
    dws_error = None

    try:
        pts_ok = await pts_client.verify_pts_token()
    except Exception as e:
        pts_error = str(e)

    try:
        dws_ok = await dingtalk_client.check_dws_available()
    except Exception as e:
        dws_error = str(e)

    try:
        yunji_result = await yunji_client.verify_session()
    except Exception as e:
        yunji_result = {"valid": False, "reason": str(e)}

    status = "healthy" if (pts_ok and dws_ok and yunji_result.get("valid")) else "degraded"
    return {
        "status": status,
        "services": {
            "pts_api": {"ok": pts_ok, "error": pts_error},
            "dws_cli": {"ok": dws_ok, "error": dws_error},
            "yunji_session": yunji_result,
        },
    }
