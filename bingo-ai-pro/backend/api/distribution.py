from __future__ import annotations

from datetime import date as date_type
from hashlib import sha256

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from database.official_draw_store import get_official_draws_by_date

router = APIRouter(prefix="/api", tags=["distribution"])


@router.get("/distribution")
def distribution(
    request: Request,
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    compact: bool = False,
) -> Response:
    date_type.fromisoformat(date)
    draws = get_official_draws_by_date(date)
    if compact:
        fields = ("issue", "numbers", "super_number", "draw_time")
        draws = [{key: draw.get(key) for key in fields} for draw in draws]
    response = JSONResponse({"date": date, "count": len(draws), "draws": draws})
    # Hash the complete representation so corrections to older rows also refresh.
    etag = '"' + sha256(response.body).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    candidates = request.headers.get("if-none-match", "").split(",")
    if any(value.strip().removeprefix("W/") in (etag, "*") for value in candidates):
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return response


@router.get("/distribution/events")
async def distribution_events(request: Request) -> StreamingResponse:
    from services.distribution_events import draw_events

    return StreamingResponse(
        draw_events(request), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
