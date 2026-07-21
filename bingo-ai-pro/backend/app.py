from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.adaptive_weight import router as adaptive_weight_router
from api.admin import router as admin_router
from api.analysis import router as analysis_router
from api.analysis_history import router as analysis_history_router
from api.backtest import router as backtest_router
from api.collector import router as collector_router
from api.data_quality import router as data_quality_router
from api.draws import router as draws_router
from api.laowanjia import router as laowanjia_router
from api.laowanjia_features import router as laowanjia_features_router
from api.laowanjia_v2 import router as laowanjia_v2_router
from api.learning import router as learning_router
from api.models import router as models_router
from api.next_prediction import router as next_prediction_router
from api.operations_center import router as operations_center_router
from api.pipeline import router as pipeline_router
from api.official_verification import router as official_verification_router
from api.player_dashboard import router as player_dashboard_router
from api.predictions import router as predictions_router
from api.prediction_tracker import router as prediction_tracker_router
from api.recommendation_center import router as recommendation_center_router
from api.recovery import router as recovery_router
from api.releases import router as releases_router
from api.simulation import router as simulation_router
from api.simulation_evaluation import router as simulation_evaluation_router
from api.strategy_evolution import router as strategy_evolution_router
from api.strategy_ranking import router as strategy_ranking_router
from api.system_health import router as system_health_router
from api.system_status import router as system_status_router
from api.today import router as today_router
from analysis.engine import analyze_all
from analysis.recommend import build_recommendation
from collectors import collect_kuaishou_snapshot, collect_pilio_today
from database.adaptive_weight_store import init_adaptive_weight_tables
from database.analysis_store import init_analysis_tables
from database.collector_store import init_collector_tables
from database.data_quality_store import init_data_quality_tables
from database.laowanjia_feature_store import init_laowanjia_feature_tables
from database.learning_store import init_learning_tables
from database.operations_store import init_operations_tables
from database.official_draw_store import init_official_draw_tables
from database.prediction_history_store import init_prediction_history_tables
from database.prediction_tracker_store import init_prediction_tracker_tables
from database.production_scope_store import init_production_scope_tables
from database.recommendation_center_store import init_recommendation_center_tables
from database.recovery_store import init_recovery_tables
from database.release_store import init_release_tables
from database.simulation_evaluation_store import init_simulation_evaluation_tables
from database.simulation_store import init_simulation_tables
from database.strategy_evolution_store import init_strategy_evolution_tables
from database.strategy_ranking_store import init_strategy_ranking_tables
from database.system_health_store import init_system_health_tables
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
from services.data_quality import run_kuaishou_data_quality_check
from services.catch_up_service import catch_up_missing_issues
from services.collector_runtime import mark_scheduler_event, refresh_system_status_cache
from services.health_cache_engine import refresh_health_cache, warm_health_cache
from services.latest_sync import HISTORICAL_CATCHUP_ENABLED, LATEST_ISSUE_PRIORITY, get_latest_sync_snapshot
from services.official_verification import collect_official_today
from services.daily_recovery import (
    DAILY_RECOVERY_ENABLED,
    DAILY_RECOVERY_HOUR,
    DAILY_RECOVERY_MINUTE,
    DAILY_RECOVERY_TIMEZONE,
    run_daily_recovery,
)

DIST_DIR = ROOT.parent / "frontend" / "dist"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="Bingo AI Pro API")
STARTUP_TIME = datetime.now(timezone.utc).isoformat()
print("startup_import_completed host=0.0.0.0 port_env=PORT")

app.include_router(adaptive_weight_router)
app.include_router(admin_router)
app.include_router(draws_router)
app.include_router(analysis_router)
app.include_router(analysis_history_router)
app.include_router(collector_router)
app.include_router(data_quality_router)
app.include_router(laowanjia_router)
app.include_router(laowanjia_features_router)
app.include_router(laowanjia_v2_router)
app.include_router(learning_router)
app.include_router(models_router)
app.include_router(next_prediction_router)
app.include_router(operations_center_router)
app.include_router(pipeline_router)
app.include_router(official_verification_router)
app.include_router(player_dashboard_router)
app.include_router(predictions_router)
app.include_router(prediction_tracker_router)
app.include_router(recommendation_center_router)
app.include_router(recovery_router)
app.include_router(releases_router)
app.include_router(simulation_router)
app.include_router(simulation_evaluation_router)
app.include_router(strategy_evolution_router)
app.include_router(strategy_ranking_router)
app.include_router(system_health_router)
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
app.state.instance_started_at = STARTUP_TIME
app.state.last_health_request_at = None
app.state.last_health_request_method = None
app.state.health_request_count_since_start = 0
app.state.last_health_user_agent = None
app.state.wake_source = "unknown"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wake_source(user_agent: str | None) -> str:
    text = (user_agent or "").lower()
    if "bingo-ai-pro-github-actions-keep-awake" in text or "github" in text:
        return "github-actions"
    if "uptimerobot" in text:
        return "uptimerobot"
    if "cron" in text:
        return "cron-job"
    if any(marker in text for marker in ("mozilla", "chrome", "safari", "firefox", "edge")):
        return "browser"
    return "unknown"


def _record_health_request(request: Request) -> None:
    user_agent = (request.headers.get("user-agent") or "")[:200]
    app.state.last_health_request_at = _utc_now_iso()
    app.state.last_health_request_method = request.method
    app.state.health_request_count_since_start = int(app.state.health_request_count_since_start or 0) + 1
    app.state.last_health_user_agent = user_agent
    app.state.wake_source = _wake_source(user_agent)


def _seconds_since(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _wake_status(seconds_since_last: int | None) -> str:
    if seconds_since_last is None:
        return "unknown"
    if seconds_since_last <= 7 * 60:
        return "healthy"
    if seconds_since_last <= 12 * 60:
        return "delayed"
    return "at_risk"


def _scheduler_listener(event) -> None:
    job_id = getattr(event, "job_id", None)
    if event.code == EVENT_JOB_EXECUTED:
        mark_scheduler_event("success", job_id)
    elif event.code == EVENT_JOB_ERROR:
        mark_scheduler_event("error", job_id, getattr(event, "exception", None))
    elif event.code == EVENT_JOB_MISSED:
        mark_scheduler_event("missed", job_id)
    elif event.code == EVENT_JOB_MAX_INSTANCES:
        mark_scheduler_event("max_instances", job_id)


def _ensure_scheduler_listener() -> None:
    if getattr(app.state, "scheduler_listener_registered", False):
        return
    scheduler.add_listener(
        _scheduler_listener,
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
    )
    app.state.scheduler_listener_registered = True


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
    startup_started = datetime.utcnow()
    print("startup_scheduler_registered background_jobs_deferred=true")
    print(f"WAKE_MONITOR instance_started_at={STARTUP_TIME}")
    print(
        "LATEST_SYNC_MODE "
        f"latest_issue_priority={str(LATEST_ISSUE_PRIORITY).lower()} "
        f"historical_catchup_enabled={str(HISTORICAL_CATCHUP_ENABLED).lower()}"
    )
    init_db()
    _ensure_scheduler_listener()

    try:
        init_collector_tables()
        init_analysis_tables()
        init_data_quality_tables()
        init_simulation_tables()
        init_simulation_evaluation_tables()
        init_adaptive_weight_tables()
        init_strategy_ranking_tables()
        init_strategy_evolution_tables()
        init_system_health_tables()
        init_operations_tables()
        init_production_scope_tables()
        init_official_draw_tables()
        init_prediction_history_tables()
        init_learning_tables()
        init_recommendation_center_tables()
        init_recovery_tables()
        init_release_tables()
        init_laowanjia_feature_tables()
        init_prediction_tracker_tables()
        try:
            warm_health_cache()
        except Exception as exc:
            print(f"Health cache warm-up failed: {exc}")
        scheduler.add_job(
            refresh_health_cache,
            "date",
            run_date=datetime.utcnow() + timedelta(seconds=5),
            id="system_health_cache_startup",
            replace_existing=True,
        )
        scheduler.add_job(
            refresh_health_cache,
            "interval",
            minutes=5,
            id="system_health_cache_refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            refresh_system_status_cache,
            "date",
            run_date=datetime.utcnow() + timedelta(seconds=5),
            id="system_status_runtime_cache_startup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=90,
        )
        scheduler.add_job(
            refresh_system_status_cache,
            "interval",
            seconds=30,
            id="system_status_runtime_cache_refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=90,
        )
        if HISTORICAL_CATCHUP_ENABLED:
            scheduler.add_job(
                catch_up_missing_issues,
                "date",
                run_date=datetime.utcnow() + timedelta(seconds=8),
                id="collector_official_catch_up_startup",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=90,
            )
        scheduler.add_job(
            collect_pilio_today,
            "date",
            run_date=datetime.utcnow() + timedelta(seconds=3),
            id="collector_pilio_startup",
            replace_existing=True,
        )
        scheduler.add_job(
            collect_kuaishou_snapshot,
            "interval",
            minutes=5,
            id="collector_kuaishou_snapshot",
            replace_existing=True,
        )
        scheduler.add_job(
            collect_pilio_today,
            "interval",
            hours=1,
            id="collector_pilio_today",
            replace_existing=True,
        )
        if HISTORICAL_CATCHUP_ENABLED:
            scheduler.add_job(
                catch_up_missing_issues,
                "interval",
                minutes=2,
                id="collector_official_catch_up",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=90,
            )
        scheduler.add_job(
            collect_official_today,
            "interval",
            minutes=2,
            id="collector_official_today",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=90,
        )
        scheduler.add_job(
            run_kuaishou_data_quality_check,
            "date",
            run_date=datetime.utcnow() + timedelta(seconds=15),
            id="data_quality_startup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            run_kuaishou_data_quality_check,
            "cron",
            hour=3,
            minute=0,
            id="data_quality_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        if DAILY_RECOVERY_ENABLED:
            scheduler.add_job(
                run_daily_recovery,
                "cron",
                hour=DAILY_RECOVERY_HOUR,
                minute=DAILY_RECOVERY_MINUTE,
                timezone=DAILY_RECOVERY_TIMEZONE,
                id="daily_recovery",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
    except Exception as exc:
        print(f"Collector scheduler setup failed: {exc}")

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

    if not scheduler.running:
        scheduler.start()
    duration_ms = round((datetime.utcnow() - startup_started).total_seconds() * 1000, 2)
    print(
        f"startup_application_ready duration_ms={duration_ms} "
        f"scheduler_running={scheduler.running} background_jobs_deferred=true"
    )
    print(
        f"startup_background_jobs_scheduled duration_ms={duration_ms} "
        f"scheduler_running={scheduler.running} background_jobs_deferred=true "
        f"startup_recovery_delay_seconds=8 system_status_cache_delay_seconds=5"
    )


@app.on_event("shutdown")
def shutdown_event() -> None:
    latest_sync = get_latest_sync_snapshot()
    print("WAKE_MONITOR shutting_down")
    print(
        f"last_health_request_at={app.state.last_health_request_at} "
        f"request_count={app.state.health_request_count_since_start} "
        f"last_collector_success_at={latest_sync.get('latest_saved_at')} "
        f"database_latest_issue={latest_sync.get('database_latest_issue')}"
    )
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
def api_health(request: Request) -> dict[str, str | int]:
    _record_health_request(request)
    started_seconds = _seconds_since(STARTUP_TIME)
    return {
        "status": "ok",
        "service": "bingo-ai-pro",
        "timestamp": _utc_now_iso(),
        "instance_started_at": STARTUP_TIME,
        "uptime_seconds": started_seconds or 0,
    }


@app.head("/api/health")
def api_health_head(request: Request) -> JSONResponse:
    _record_health_request(request)
    return JSONResponse(status_code=200, content=None)


@app.get("/api/health/wake-status")
def api_health_wake_status() -> dict[str, str | int | None]:
    seconds = _seconds_since(app.state.last_health_request_at)
    return {
        "instance_started_at": STARTUP_TIME,
        "last_health_request_at": app.state.last_health_request_at,
        "seconds_since_last_health_request": seconds,
        "health_request_count_since_start": app.state.health_request_count_since_start,
        "last_health_request_method": app.state.last_health_request_method,
        "last_health_user_agent": app.state.last_health_user_agent,
        "wake_source": app.state.wake_source,
        "wake_status": _wake_status(seconds),
    }


@app.get("/dashboard")
def dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.head("/dashboard")
def dashboard_head() -> JSONResponse:
    return JSONResponse(status_code=200, content=None)


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


if DIST_DIR.exists():
    app.mount("/", StaticFiles(directory=DIST_DIR, html=True), name="frontend")
