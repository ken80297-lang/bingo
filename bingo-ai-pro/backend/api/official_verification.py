from fastapi import APIRouter

from services.official_verification import (
    collect_official_today,
    official_history,
    official_latest,
    official_statistics,
    official_verification_history,
    official_verification_latest,
)

router = APIRouter(prefix="/api/official", tags=["Official Verification"])


@router.get("/latest")
def api_official_latest():
    return official_latest()


@router.get("/history")
def api_official_history(limit: int = 30):
    return official_history(limit)


@router.post("/collect-today")
def api_official_collect_today():
    return collect_official_today()


@router.get("/verification/latest")
def api_official_verification_latest():
    return official_verification_latest()


@router.get("/verification/history")
def api_official_verification_history(limit: int = 30):
    return official_verification_history(limit)


@router.get("/statistics")
def api_official_statistics():
    return official_statistics()
