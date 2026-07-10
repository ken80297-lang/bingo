from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from collectors.taiwan_lottery_collector import fetch_official_bingo_results
from database.collector_store import (
    get_draw_history,
    get_kuaishou_history,
    get_latest_kuaishou_snapshot,
)
from database.official_draw_store import (
    get_latest_official_draw,
    get_latest_verification,
    get_official_draw_by_issue,
    get_official_draw_history,
    get_official_statistics_counts,
    get_verification_history,
    save_draw_verification,
    save_draw_verifications,
    save_official_draws,
)
from services.operations_center import record_operation_event
from database.prediction_history_store import update_prediction_history_result

logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _today_taipei() -> date:
    return datetime.now(TAIPEI_TZ).date()


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if 1 <= number <= 80 else None


def _as_int_list(values: Any) -> list[int]:
    numbers = []
    for value in values or []:
        number = _as_int(value)
        if number is not None and number not in numbers:
            numbers.append(number)
    return sorted(numbers)


def _find_numbers(value: Any) -> list[int]:
    if isinstance(value, list):
        direct = _as_int_list(value)
        if len(direct) == 20:
            return direct
        for item in value:
            nested = _find_numbers(item)
            if len(nested) == 20:
                return nested
    if isinstance(value, dict):
        for key in ("numbers", "draw_numbers", "result", "bigShowOrder", "openShowOrder"):
            found = _as_int_list(value.get(key))
            if len(found) == 20:
                return found
        for item in value.values():
            nested = _find_numbers(item)
            if len(nested) == 20:
                return nested
    return []


def _find_super(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("super_number", "super", "bullEyeTop", "bullEye", "超級獎號"):
            number = _as_int(value.get(key))
            if number is not None:
                return number
        api_data = (((value.get("parsed_json") or {}).get("api_get_data") or {}).get("data") or [])
        if api_data:
            number = _find_super(api_data[0])
            if number is not None:
                return number
        for item in value.values():
            nested = _find_super(item)
            if nested is not None:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_super(item)
            if nested is not None:
                return nested
    return None


def _record_event(
    component: str,
    status: str,
    issue: str | None,
    start: float,
    message: str,
    error: Exception | None = None,
) -> None:
    try:
        record_operation_event(
            component=component,
            event_type="pipeline_stage",
            status=status,
            issue=issue,
            message=message,
            duration_ms=_duration_ms(start),
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )
    except Exception:
        logger.exception("official operation event failed")


def _local_draw_from_record(record: dict) -> dict | None:
    numbers = _as_int_list(record.get("numbers")) or _find_numbers(record.get("parsed_json") or record)
    if len(numbers) != 20:
        return None
    return {
        "issue": str(record.get("issue")),
        "numbers": numbers,
        "super_number": _as_int(record.get("super_number")) or _find_super(record.get("parsed_json") or record),
    }


def _load_local_draw(issue: str) -> dict | None:
    target = str(issue)
    try:
        for item in get_draw_history(300):
            if str(item.get("issue")) == target:
                draw = _local_draw_from_record(item)
                if draw:
                    return draw
    except Exception:
        logger.exception("failed to load draw_history for official verification")

    try:
        for item in get_kuaishou_history(300):
            if str(item.get("issue")) == target:
                draw = _local_draw_from_record(item)
                if draw:
                    return draw
    except Exception:
        logger.exception("failed to load kuaishou history for official verification")

    return None


def _load_local_draw_map(limit: int = 50) -> dict[str, dict]:
    draws: dict[str, dict] = {}
    try:
        for item in get_draw_history(limit):
            draw = _local_draw_from_record(item)
            if draw and draw.get("issue"):
                draws[str(draw["issue"])] = draw
    except Exception:
        logger.exception("failed to load draw_history map for official verification")

    try:
        for item in get_kuaishou_history(limit):
            draw = _local_draw_from_record(item)
            if draw and draw.get("issue"):
                draws.setdefault(str(draw["issue"]), draw)
    except Exception:
        logger.exception("failed to load kuaishou history map for official verification")
    return draws


def _verification_payload(issue: str, local_draw: dict | None, official_draw: dict | None) -> dict:
    if not official_draw:
        local_numbers = _as_int_list((local_draw or {}).get("numbers", []))
        return {
            "issue": str(issue),
            "kuaishou_numbers": local_numbers,
            "official_numbers": [],
            "kuaishou_super": (local_draw or {}).get("super_number"),
            "official_super": None,
            "numbers_match": False,
            "super_match": False,
            "verified": False,
            "status": "waiting_official",
            "verified_at": None,
        }

    official_numbers = _as_int_list(official_draw.get("numbers"))
    kuaishou_numbers = _as_int_list((local_draw or {}).get("numbers", []))
    kuaishou_super = _as_int((local_draw or {}).get("super_number"))
    official_super = _as_int(official_draw.get("super_number"))
    numbers_match = bool(len(kuaishou_numbers) == 20 and len(official_numbers) == 20 and kuaishou_numbers == official_numbers)
    super_match = bool(kuaishou_super is not None and official_super is not None and kuaishou_super == official_super)
    verified = False
    if len(official_numbers) != 20:
        status = "waiting_official"
    elif len(kuaishou_numbers) != 20:
        status = "waiting_kuaishou"
    elif kuaishou_super is None or official_super is None:
        status = "waiting_super_number"
    elif numbers_match and super_match:
        status = "verified"
        verified = True
    else:
        status = "mismatch"

    return {
        "issue": str(issue),
        "kuaishou_numbers": kuaishou_numbers,
        "official_numbers": official_numbers,
        "kuaishou_super": kuaishou_super,
        "official_super": official_super,
        "numbers_match": numbers_match,
        "super_match": super_match,
        "verified": verified,
        "status": status,
        "verified_at": datetime.utcnow().isoformat() if verified else None,
    }


def run_official_verification(limit: int = 10) -> dict:
    start = time.perf_counter()
    try:
        saved = []
        limit = max(1, min(int(limit or 10), 10))
        local_draws = _load_local_draw_map(30)
        for official in get_official_draw_history(limit):
            local_draw = local_draws.get(str(official.get("issue")))
            payload = _verification_payload(official.get("issue"), local_draw, official)
            payload["saved"] = save_draw_verification(payload)
            try:
                payload["prediction_history"] = update_prediction_history_result(
                    {
                        "issue": official.get("issue"),
                        "numbers": official.get("numbers"),
                        "super_number": official.get("super_number"),
                    }
                )
            except Exception as exc:
                logger.exception("prediction_history update failed during official verification")
                payload["prediction_history"] = {"status": "error", "message": str(exc)}
            try:
                from services.analysis_engine import analyze_official_draw

                payload["analysis_engine"] = analyze_official_draw(official)
            except Exception as exc:
                logger.exception("analysis engine update failed during official verification")
                payload["analysis_engine"] = {"status": "error", "message": str(exc)}
            saved.append(payload)

        latest_kuaishou = get_latest_kuaishou_snapshot()
        if latest_kuaishou and latest_kuaishou.get("issue"):
            issue = str(latest_kuaishou.get("issue"))
            if not get_official_draw_by_issue(issue):
                local_draw = _local_draw_from_record(latest_kuaishou)
                payload = _verification_payload(issue, local_draw, None)
                payload["saved"] = save_draw_verification(payload)
                saved.insert(0, payload)

        latest = get_latest_verification()
        _record_event(
            "official_verification",
            "ok",
            latest.get("issue") if latest else None,
            start,
            f"verified {len(saved)} official draws",
        )
        return {"status": "ok", "count": len(saved), "latest": latest, "data": saved}
    except Exception as exc:
        logger.exception("official verification failed")
        _record_event("official_verification", "error", None, start, "official verification failed", exc)
        return {"status": "error", "count": 0, "error": str(exc), "data": []}


def reverify_recent_draws(limit: int = 200) -> dict:
    start = time.perf_counter()
    try:
        limit = max(1, min(int(limit or 200), 200))
        local_draws = _load_local_draw_map(limit)
        saved = []
        status_counts: dict[str, int] = {}
        for official in get_official_draw_history(limit):
            local_draw = local_draws.get(str(official.get("issue")))
            payload = _verification_payload(official.get("issue"), local_draw, official)
            saved.append(payload)
            status = payload.get("status") or "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
        batch_saved = save_draw_verifications(saved)
        _record_event(
            "official_verification",
            "ok",
            saved[0].get("issue") if saved else None,
            start,
            f"reverified {len(saved)} official draws",
        )
        return {
            "status": "ok",
            "count": len(saved),
            "status_counts": status_counts,
            "saved": batch_saved,
            "elapsed_seconds": round(time.perf_counter() - start, 3),
            "data": saved[:20],
        }
    except Exception as exc:
        logger.exception("official reverify failed")
        _record_event("official_verification", "error", None, start, "official reverify failed", exc)
        return {"status": "error", "count": 0, "error": str(exc), "data": []}


def collect_official_today() -> dict:
    start = time.perf_counter()
    try:
        draws = fetch_official_bingo_results(_today_taipei(), page_num=1, page_size=10)
        saved = save_official_draws(draws)
        latest_issue = draws[0].get("issue") if draws else None
        _record_event(
            "official_collector",
            "ok" if saved.get("status") == "ok" else "warning",
            latest_issue,
            start,
            f"official collector saved {saved.get('saved', 0)} draws",
        )
        verification = run_official_verification(limit=10)
        reverify = reverify_recent_draws(limit=20)
        prediction = {"status": "unknown"}
        try:
            from services.prediction_tracker import evaluate_pending_predictions

            prediction = evaluate_pending_predictions(max_runs=3)
        except Exception as exc:
            logger.exception("official prediction evaluation failed")
            prediction = {"status": "error", "message": str(exc)}
        return {
            "status": "ok" if saved.get("status") == "ok" else "warning",
            "count": len(draws),
            "saved": saved,
            "verification": verification,
            "reverify": reverify,
            "prediction": prediction,
        }
    except Exception as exc:
        logger.exception("official collector failed")
        _record_event("official_collector", "error", None, start, "official collector failed", exc)
        return {"status": "error", "count": 0, "error": str(exc)}


def official_latest() -> dict:
    return {"status": "ok", "data": get_latest_official_draw()}


def official_history(limit: int = 30) -> dict:
    return {"status": "ok", "data": get_official_draw_history(limit)}


def official_verification_latest() -> dict:
    return {"status": "ok", "data": get_latest_verification()}


def official_verification_history(limit: int = 30) -> dict:
    return {"status": "ok", "data": get_verification_history(limit)}


def official_statistics() -> dict:
    latest_official = get_latest_official_draw()
    latest_kuaishou = get_latest_kuaishou_snapshot()
    counts = get_official_statistics_counts()
    total = counts.get("verified_count", 0) + counts.get("mismatch_count", 0)
    verified_rate = round((counts.get("verified_count", 0) / total) * 100, 2) if total else 0
    return {
        "status": "ok",
        "latest_official_issue": latest_official.get("issue") if latest_official else None,
        "latest_kuaishou_issue": latest_kuaishou.get("issue") if latest_kuaishou else None,
        "verified_count": counts.get("verified_count", 0),
        "mismatch_count": counts.get("mismatch_count", 0),
        "waiting_kuaishou_count": counts.get("waiting_kuaishou_count", 0),
        "waiting_official_count": counts.get("waiting_official_count", 0),
        "waiting_super_number_count": counts.get("waiting_super_number_count", 0),
        "waiting_count": counts.get("waiting_count", 0),
        "total_count": counts.get("total_count", 0),
        "verified_rate": verified_rate,
    }
