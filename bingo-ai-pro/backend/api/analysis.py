from fastapi import APIRouter

from analysis.engine import analyze_all

router = APIRouter(prefix="/api", tags=["analysis"])


@router.get("/analysis")
def api_analysis(limit: int = 50):
    return analyze_all(limit)