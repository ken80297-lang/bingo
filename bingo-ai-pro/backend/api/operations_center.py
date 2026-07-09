from fastapi import APIRouter

from services.operations_center import (
    operation_database_health,
    operation_errors,
    operation_metrics,
    operation_summary,
    operation_timeline,
)

router = APIRouter(prefix="/api/operations", tags=["Operations Center"])


@router.get("/timeline")
def api_operations_timeline(limit: int = 50):
    return operation_timeline(limit)


@router.get("/errors")
def api_operations_errors(limit: int = 50):
    return operation_errors(limit)


@router.get("/metrics")
def api_operations_metrics():
    return operation_metrics()


@router.get("/database-health")
def api_operations_database_health():
    return operation_database_health()


@router.get("/summary")
def api_operations_summary():
    return operation_summary()
