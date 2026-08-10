from fastapi import APIRouter

from services.official_verification import (
    collect_official_today,
    official_history,
    official_latest,
    official_statistics,
    official_summary_history,
    official_verification_history,
    official_verification_latest,
    reverify_recent_draws,
)

router = APIRouter(prefix="/api/official", tags=["Official Verification"])


@router.get("/latest")
def api_official_latest():
    return official_latest()


@router.get("/history")
def api_official_history(limit: int | None = None, view: str = "full"):
    normalized_view = str(view or "full").strip().lower()
    if normalized_view == "summary":
        return official_summary_history(max(1, min(int(limit or 20), 100)))
    payload = official_history(max(1, min(int(limit or 30), 200)))
    payload["view"] = "full"
    return payload


@router.post("/collect-today")
def api_official_collect_today():
    return collect_official_today()


@router.get("/verification/latest")
def api_official_verification_latest():
    return official_verification_latest()


@router.get("/verification/history")
def api_official_verification_history(limit: int = 30):
    return official_verification_history(limit)


@router.post("/reverify")
def api_official_reverify(limit: int = 200):
    return reverify_recent_draws(limit)


@router.get("/statistics")
def api_official_statistics():
    return official_statistics()
