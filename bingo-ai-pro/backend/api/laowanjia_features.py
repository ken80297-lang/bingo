from fastapi import APIRouter

from database.laowanjia_feature_store import (
    get_laowanjia_feature_history,
    get_latest_laowanjia_feature,
)
from services.laowanjia_features import run_laowanjia_feature_analysis

router = APIRouter(prefix="/api/laowanjia-features", tags=["Laowanjia Features"])


@router.post("/run")
def api_laowanjia_features_run():
    return run_laowanjia_feature_analysis()


@router.get("/latest")
def api_laowanjia_features_latest():
    return {
        "status": "ok",
        "data": get_latest_laowanjia_feature(),
    }


@router.get("/history")
def api_laowanjia_features_history(limit: int = 50):
    return {
        "status": "ok",
        "data": get_laowanjia_feature_history(limit),
    }

