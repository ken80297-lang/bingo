from fastapi import APIRouter

from database.data_quality_store import (
    get_data_quality_reports,
    get_latest_data_quality_report,
)
from services.data_quality import run_kuaishou_data_quality_check

router = APIRouter(prefix="/api/data-quality", tags=["Data Quality"])


@router.get("/latest")
def api_data_quality_latest():
    return {
        "status": "ok",
        "data": get_latest_data_quality_report(),
    }


@router.get("/history")
def api_data_quality_history(limit: int = 30):
    return {
        "status": "ok",
        "data": get_data_quality_reports(limit),
    }


@router.post("/run")
def api_data_quality_run():
    return run_kuaishou_data_quality_check()
