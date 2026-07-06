from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.analysis import router as analysis_router
from api.backtest import router as backtest_router
from api.draws import router as draws_router
from api.laowanjia import router as laowanjia_router
from api.laowanjia_v2 import router as laowanjia_v2_router
from api.system_status import router as system_status_router
from api.today import router as today_router
from analysis.engine import analyze_all
from analysis.recommend import build_recommendation
from database import get_connection
from db import (
    fetch_latest_draws,
    get_analysis_by_issue,
    get_history_draws,
    get_latest_draw,
    get_recommendation_by_issue,
    get_statistics,
    init_db,
    save_analysis_result,
    save_draws,
    save_recommendation_result,
    save_statistics,
)

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT.parent / "frontend" / "dist"

app = FastAPI(title="Bingo AI Pro API")

try:
    conn = get_connection()
    print("✅ Supabase 連線成功")
    conn.close()
except Exception as e:
    print("❌ Supabase 連線失敗")
    print(e)

app.include_router(draws_router)
app.include_router(analysis_router)
app.include_router(laowanjia_router)
app.include_router(laowanjia_v2_router)
app.include_router(system_status_router)
app.include_router(today_router)
app.include_router(backtest_router)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATE: dict[str, str | int | None] = {
    "last_update": "-",
    "last_added": 0,
    "last_error": None,
}

scheduler = BackgroundScheduler()
app.state.scheduler = scheduler


def summary_statistics(draws: list[dict]) -> dict:
    return {
        "total_draws": len(draws),
        "latest_issue": draws[0]["issue"] if draws else None,
    }


def refresh_data() -> dict[str, object]:
    init_db()

    draws = fetch_latest_draws()
    added = save_draws(draws)

    recent = get_history_draws(limit=120)
    if not recent:
        raise RuntimeError("無法讀取歷史資料")

    analysis = analyze_all(limit=120)
    recommendation = build_recommendation(recent, analysis)
    latest_issue = recent[0]["issue"]

    save_analysis_result(latest_issue, analysis)
    save_recommendation_result(latest_issue, recommendation)

    stats = summary_statistics(recent)
    save_statistics("latest_issue", latest_issue)
    save_statistics("last_update", datetime.utcnow().isoformat())
    save_statistics("total_draws", str(len(recent)))
    save_statistics("updated_at", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    for key, value in stats.items():
        save_statistics(key, json.dumps(value, ensure_ascii=False))

    STATE["last_update"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    STATE["last_added"] = added
    STATE["last_error"] = None

    return {
        "added": added,
        "analysis": analysis,
        "recommendation": recommendation,
        "statistics": stats,
        "state": STATE,
    }


@app.on_event("startup")
def startup_event() -> None:
    init_db()

    scheduler.add_job(
        refresh_data,
        "date",
        run_date=datetime.utcnow() + timedelta(seconds=10),
        id="first_refresh",
        replace_existing=True,
    )

    scheduler.add_job(
        refresh_data,
        "interval",
        minutes=5,
        id="refresh_job",
        replace_existing=True,
    )

    scheduler.start()


@app.on_event("shutdown")
def shutdown_event() -> None:
    scheduler.shutdown(wait=False)


@app.get("/api/latest")
def api_latest() -> JSONResponse:
    latest = get_latest_draw()
    if not latest:
        raise HTTPException(status_code=404, detail="找不到最新開獎資料")

    recent = get_history_draws(limit=120)
    analysis = get_analysis_by_issue(latest["issue"]) or analyze_all(limit=120)
    recommendation = get_recommendation_by_issue(latest["issue"]) or build_recommendation(
        recent,
        analysis,
    )
    stats = get_statistics()

    return JSONResponse(
        {
            "latest": latest,
            "analysis": analysis,
            "recommendation": recommendation,
            "statistics": stats,
            "state": STATE,
        }
    )


@app.get("/api/history")
def api_history(limit: int = 80) -> JSONResponse:
    history = get_history_draws(limit=limit)
    stats = get_statistics()
    return JSONResponse(
        {
            "history": history,
            "statistics": stats,
            "state": STATE,
        }
    )


@app.get("/api/analyze")
def api_analyze() -> JSONResponse:
    analysis = analyze_all(limit=120)
    return JSONResponse(
        {
            "analysis": analysis,
            "state": STATE,
        }
    )


@app.get("/api/recommend")
def api_recommend() -> JSONResponse:
    recent = get_history_draws(limit=120)
    analysis = analyze_all(limit=120)
    recommendation = build_recommendation(recent, analysis)

    return JSONResponse(
        {
            "recommendation": recommendation,
            "state": STATE,
        }
    )


@app.post("/api/update")
def api_update() -> JSONResponse:
    try:
        payload = refresh_data()
        return JSONResponse(payload)
    except Exception as exc:
        STATE["last_error"] = str(exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"status": "ok"}


if DIST_DIR.exists():
    app.mount("/", StaticFiles(directory=DIST_DIR, html=True), name="frontend")
