import logging

from fastapi import APIRouter

from services.player_dashboard import build_player_dashboard_summary

router = APIRouter(prefix="/api/dashboard", tags=["Player Dashboard"])
logger = logging.getLogger(__name__)


@router.get("/player-summary")
def api_player_dashboard_summary():
    try:
        return build_player_dashboard_summary()
    except Exception:
        logger.exception("player dashboard summary failed")
        return {
            "status": "unknown",
            "current_draw": None,
            "sync": {"is_synced": False, "lag_count": None},
            "next_prediction": {},
            "previous_verification": None,
            "data_counts": {},
            "history": {},
            "warnings": ["player summary unavailable"],
        }
