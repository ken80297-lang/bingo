from __future__ import annotations

import os

from fastapi import APIRouter, Request

from config.runtime_flags import get_scheduler_runtime_flags
from database.collector_store import get_collector_db_path_status

router = APIRouter(prefix="/api", tags=["Runtime Diagnostics"])


def _render_metadata() -> dict[str, str | None]:
    return {
        "git_commit": os.getenv("RENDER_GIT_COMMIT"),
        "service_id": os.getenv("RENDER_SERVICE_ID"),
        "service_name": os.getenv("RENDER_SERVICE_NAME"),
        "instance_id": os.getenv("RENDER_INSTANCE_ID"),
    }


@router.get("/runtime-diagnostics")
def api_runtime_diagnostics(request: Request) -> dict:
    return {
        "status": "ok",
        "instance_started_at": getattr(request.app.state, "instance_started_at", None),
        "render": _render_metadata(),
        "scheduler_flags": get_scheduler_runtime_flags(),
        "collector_db_path": get_collector_db_path_status(),
    }
