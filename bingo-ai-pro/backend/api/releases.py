from __future__ import annotations

from fastapi import APIRouter

from database.release_store import (
    activate_release,
    get_current_release,
    get_release_by_version,
    get_release_for_issue,
    list_releases,
    register_release,
    rollback_readiness,
)

router = APIRouter(prefix="/api", tags=["Releases"])


@router.get("/releases/current")
def api_current_release():
    return {"status": "ok", "data": get_current_release()}


@router.get("/releases")
def api_releases(limit: int = 50):
    return {"status": "ok", "data": list_releases(limit)}


@router.get("/releases/by-issue/{issue}")
def api_release_by_issue(issue: str):
    return get_release_for_issue(issue)


@router.get("/releases/{release_version}")
def api_release(release_version: str):
    release = get_release_by_version(release_version)
    return {"status": "ok" if release else "not_found", "data": release}


@router.get("/releases/{release_version}/rollback-readiness")
def api_rollback_readiness(release_version: str):
    return rollback_readiness(release_version)


@router.post("/admin/releases/register")
def api_register_release(payload: dict):
    return register_release(payload, activate=bool(payload.get("is_active")))


@router.post("/admin/releases/{release_version}/activate")
def api_activate_release(release_version: str):
    return activate_release(release_version)

