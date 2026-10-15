from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from database.official_draw_store import get_official_draws_by_date

router = APIRouter(prefix="/api", tags=["distribution"])


@router.get("/distribution")
def distribution(date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")) -> JSONResponse:
    # Validate calendar dates as well as the wire format.
    date_type.fromisoformat(date)
    draws = get_official_draws_by_date(date)
    return JSONResponse({"date": date, "count": len(draws), "draws": draws})


@router.get("/distribution/events")
async def distribution_events(request: Request) -> StreamingResponse:
    from services.distribution_events import draw_events

    return StreamingResponse(
        draw_events(request), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
