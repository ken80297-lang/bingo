from fastapi import APIRouter

from database.analysis_store import (
    get_analysis_history,
    get_analysis_summary_records,
    get_analysis_statistics,
    get_latest_analysis_history,
)

router = APIRouter(prefix="/api/analysis", tags=["Analysis History"])


@router.get("/latest")
def api_analysis_latest():
    return {
        "status": "ok",
        "data": get_latest_analysis_history(),
    }


@router.get("/history")
def api_analysis_history(limit: int | None = None, view: str = "full"):
    normalized_view = str(view or "full").strip().lower()
    if normalized_view == "summary":
        safe_limit = max(1, min(int(limit or 20), 100))
        data = get_analysis_summary_records(safe_limit)
    else:
        safe_limit = max(1, min(int(limit or 100), 500))
        data = get_analysis_history(safe_limit)
    return {
        "status": "ok",
        "view": "summary" if normalized_view == "summary" else "full",
        "data": data,
    }


@router.get("/statistics")
def api_analysis_statistics(limit: int = 100):
    safe_limit = max(1, min(int(limit or 100), 500))
    return {
        "status": "ok",
        "data": get_analysis_statistics(safe_limit),
    }
