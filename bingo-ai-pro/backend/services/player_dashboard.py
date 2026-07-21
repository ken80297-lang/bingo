from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from database.collector_store import get_latest_kuaishou_snapshot
from database.analysis_store import get_latest_analysis_history
from database.official_draw_store import get_latest_official_draw, get_official_draw_by_issue
from database.operations_store import get_latest_operation_event
from database.prediction_history_store import get_prediction_history_records
from database.prediction_history_store import get_latest_prediction_history
from database.prediction_history_store import get_prediction_for_source_target
from database.prediction_history_store import get_latest_verified_prediction_at_or_before
from database.prediction_history_store import get_prediction_history_statistics
from database.prediction_history_store import get_prediction_lifecycle_aggregates
from database.prediction_history_store import is_production_prediction
from config.production_scope import production_scope_payload
from database.release_store import get_current_release
from services.prediction_refresh import prediction_refresh_status

logger = logging.getLogger(__name__)

PLAYER_SUMMARY_TTL_SECONDS = 30
PLAYER_DASHBOARD_QUERY_TIMEOUT_SECONDS = 2
PLAYER_DASHBOARD_HISTORY_LIMIT = 10
_PLAYER_SUMMARY_CACHE: dict[str, Any] = {"payload": None, "expires_at": 0.0}
_PLAYER_COMPONENT_CACHE: dict[str, Any] = {
    "official_draw": None,
    "latest_prediction": None,
    "prediction_history": [],
}
PLAYER_CACHE_FILTER_VERSION = "production_prediction_v2"
_PLAYER_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="player-dashboard")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cached_summary() -> dict | None:
    payload = _PLAYER_SUMMARY_CACHE.get("payload")
    expires_at = float(_PLAYER_SUMMARY_CACHE.get("expires_at") or 0)
    if isinstance(payload, dict) and time.monotonic() < expires_at:
        if payload.get("cache_filter_version") != PLAYER_CACHE_FILTER_VERSION:
            return None
        latest = ((payload.get("next_prediction") or {}).get("prediction_issue"))
        based_on = ((payload.get("next_prediction") or {}).get("based_on_issue"))
        if latest and not is_production_prediction({"issue": based_on, "prediction_issue": latest, "recommend_numbers": (payload.get("next_prediction") or {}).get("recommend_numbers")}):
            return None
        next_prediction = payload.get("next_prediction") or {}
        if next_prediction.get("status") == "expired" or int(next_prediction.get("lag_issues") or 0) > 1:
            return None
        cached = deepcopy(payload)
        cached["cached"] = True
        return cached
    return None


def _store_summary_cache(payload: dict) -> None:
    _PLAYER_SUMMARY_CACHE["payload"] = deepcopy(payload)
    _PLAYER_SUMMARY_CACHE["expires_at"] = time.monotonic() + PLAYER_SUMMARY_TTL_SECONDS


def _store_component_cache(name: str, payload: Any) -> None:
    if name == "latest_prediction" and payload and not is_production_prediction(payload):
        return
    if name == "prediction_history" and isinstance(payload, list):
        payload = [item for item in payload if is_production_prediction(item)]
    _PLAYER_COMPONENT_CACHE[name] = deepcopy(payload)


def _load_component_cache(name: str, fallback=None):
    cached = _PLAYER_COMPONENT_CACHE.get(name)
    if cached is None:
        return fallback
    if name == "latest_prediction" and cached and not is_production_prediction(cached):
        return fallback
    if name == "prediction_history" and isinstance(cached, list):
        cached = [item for item in cached if is_production_prediction(item)]
    return deepcopy(cached)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _as_int_list(values: Any) -> list[int]:
    result: list[int] = []
    if isinstance(values, str):
        try:
            import json

            parsed = json.loads(values)
            values = parsed if isinstance(parsed, list) else [values]
        except Exception:
            values = [values]
    for value in values or []:
        number = _as_int(value)
        if number is not None and 1 <= number <= 80 and number not in result:
            result.append(number)
    return sorted(result)


def _valid_production_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.upper().startswith("TEST") or text.startswith("99"):
        return None
    if not text.isdigit():
        return None
    return text


def _derive_next_issue(source_issue: Any) -> str | None:
    issue = _valid_production_issue(source_issue)
    if not issue:
        return None
    try:
        return str(int(issue) + 1)
    except Exception:
        return None


def _current_prediction_for_draw(current_draw: dict | None) -> dict | None:
    source_issue = (current_draw or {}).get("issue")
    target_issue = _derive_next_issue(source_issue)
    if not source_issue or not target_issue:
        return None
    try:
        record = get_prediction_for_source_target(str(source_issue), target_issue)
    except Exception:
        logger.exception("player dashboard exact latest prediction lookup failed")
        return None
    return record if is_production_prediction(record) else None


def _max_issue(*values: Any) -> str | None:
    issues = [_as_int(value) for value in values if value not in (None, "")]
    issues = [issue for issue in issues if issue is not None]
    return str(max(issues)) if issues else None


def _target_status(target_issue: Any, current_issue: Any) -> dict:
    target_text = _valid_production_issue(target_issue)
    current_text = _valid_production_issue(current_issue)
    target_int = _as_int(target_text)
    current_int = _as_int(current_text)
    if target_int is None:
        return {"is_current": False, "status": "unavailable"}
    if current_int is None:
        return {"is_current": False, "status": "unavailable"}
    if target_int > current_int:
        return {"is_current": True, "status": "ready"}
    if target_int == current_int:
        return {"is_current": False, "status": "waiting_refresh"}
    return {"is_current": False, "status": "expired"}


def _dashboard_prediction_freshness(target_issue: Any, current_issue: Any) -> dict:
    target_int = _as_int(_valid_production_issue(target_issue))
    current_int = _as_int(_valid_production_issue(current_issue))
    if target_int is None or current_int is None:
        return {
            "dashboard_status": "unavailable",
            "stale_status": "unknown",
            "stale_status_label": "unknown",
            "is_stale": True,
            "lag_issues": None,
            "expected_target_issue": None,
        }

    expected_target = current_int + 1
    lag = max(expected_target - target_int, 0)
    if lag == 0:
        return {
            "dashboard_status": "waiting_draw",
            "stale_status": "normal",
            "stale_status_label": "normal",
            "is_stale": False,
            "lag_issues": 0,
            "expected_target_issue": str(expected_target),
        }
    if lag == 1:
        return {
            "dashboard_status": "waiting_refresh",
            "stale_status": "waiting_sync",
            "stale_status_label": "waiting_sync",
            "is_stale": True,
            "lag_issues": 1,
            "expected_target_issue": str(expected_target),
        }
    return {
        "dashboard_status": "expired",
        "stale_status": "possibly_expired",
        "stale_status_label": "possibly_expired",
        "is_stale": True,
        "lag_issues": lag,
        "expected_target_issue": str(expected_target),
    }


def _parse_draw_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            continue
    return None


def _format_draw_time(value: Any) -> str | None:
    parsed = _parse_draw_datetime(value)
    if parsed:
        return parsed.strftime("%Y/%m/%d %H:%M:%S")
    return str(value) if value else None


def _based_on_time(based_on_issue: Any, based_draw: dict | None) -> dict:
    draw_time = _format_draw_time((based_draw or {}).get("draw_time"))
    if draw_time:
        return {
            "based_on_draw_time": draw_time,
            "based_on_time_source": "official_draw_time",
        }
    event = get_latest_operation_event("official_draw_saved", str(based_on_issue)) if based_on_issue else None
    event_time = _format_draw_time((event or {}).get("created_at"))
    if event_time:
        return {
            "based_on_draw_time": event_time,
            "based_on_time_source": "official_draw_saved_event",
        }
    return {
        "based_on_draw_time": None,
        "based_on_time_source": "unavailable",
    }


def _expected_draw_time(next_data: dict, current_draw: dict | None) -> tuple[str | None, str]:
    stored = (
        next_data.get("expected_draw_time")
        or next_data.get("draw_time")
        or next_data.get("prediction_time")
    )
    if stored:
        return _format_draw_time(stored), "stored"
    current_time = (current_draw or {}).get("draw_time")
    parsed = _parse_draw_datetime(current_time)
    if parsed:
        return (parsed + timedelta(minutes=5)).strftime("%Y/%m/%d %H:%M:%S"), "derived"
    return None, "unavailable"


def _big_small(numbers: list[int]) -> str | None:
    if not numbers:
        return None
    big = sum(1 for number in numbers if number >= 41)
    small = len(numbers) - big
    if big > small:
        return "big"
    if small > big:
        return "small"
    return "balanced"


def _odd_even(numbers: list[int]) -> str | None:
    if not numbers:
        return None
    odd = sum(1 for number in numbers if number % 2)
    even = len(numbers) - odd
    if odd > even:
        return "odd"
    if even > odd:
        return "even"
    return "balanced"


def _hit_label(hit_count: int) -> str:
    if hit_count >= 7:
        return "excellent"
    if hit_count >= 5:
        return "strong"
    if hit_count >= 3:
        return "moderate"
    if hit_count >= 1:
        return "low"
    return "no_hit"


def _current_draw(draw: dict | None) -> dict | None:
    if not draw:
        return None
    numbers = _as_int_list(draw.get("numbers"))
    return {
        "issue": draw.get("issue"),
        "draw_date": draw.get("draw_date"),
        "draw_time": draw.get("draw_time"),
        "numbers": numbers,
        "super_number": draw.get("super_number"),
        "big_small": draw.get("big_small") or _big_small(numbers),
        "odd_even": draw.get("odd_even") or _odd_even(numbers),
        "source": draw.get("source") or "official",
        "collected_at": draw.get("updated_at") or draw.get("created_at"),
    }


def _pairs(numbers: list[int], diff: int) -> list[list[int]]:
    number_set = set(numbers)
    return [[n, n + diff] for n in sorted(numbers) if n + diff in number_set]


def _tails(numbers: list[int]) -> list[int]:
    return sorted({number % 10 for number in numbers})


def _tail_groups(numbers: list[int]) -> list[dict]:
    return [
        {"tail": tail, "label": f"{tail}尾", "numbers": [number for number in numbers if number % 10 == tail]}
        for tail in _tails(numbers)
    ]


def _twins(numbers: list[int]) -> list[int]:
    twin_numbers = {11, 22, 33, 44, 55, 66, 77}
    return [number for number in numbers if number in twin_numbers]


def _patch_numbers(numbers: list[int]) -> list[int]:
    candidates = []
    for number in numbers:
        for diff in (1, 2, 10):
            for value in (number - diff, number + diff):
                if 1 <= value <= 80 and value not in numbers and value not in candidates:
                    candidates.append(value)
    return sorted(candidates[:8])


def _prediction_from_history(
    record: dict | None,
    current_draw: dict | None,
    detected_latest_issue: Any = None,
) -> dict | None:
    if not record:
        return None
    numbers = _as_int_list(record.get("recommend_numbers"))
    if not numbers:
        return None
    database_latest_issue = (current_draw or {}).get("issue")
    current_issue = detected_latest_issue or database_latest_issue
    based_on_issue = (
        record.get("issue")
        or current_issue
    )
    target_issue = (
        record.get("prediction_issue")
        or record.get("target_issue")
    )
    if _valid_production_issue(target_issue):
        target_issue_source = "stored"
    else:
        derived = _derive_next_issue(based_on_issue)
        target_issue = derived
        target_issue_source = "derived_from_source_issue" if derived else "unavailable"
    status = _target_status(target_issue, current_issue)
    freshness = _dashboard_prediction_freshness(target_issue, current_issue)
    if freshness.get("lag_issues") is not None and freshness.get("lag_issues") > 1:
        return None
    refresh = prediction_refresh_status(current_draw, record)
    expected_time, expected_source = _expected_draw_time(record, current_draw)
    based_draw = get_official_draw_by_issue(based_on_issue) if based_on_issue else None
    based_time = _based_on_time(based_on_issue, based_draw)
    recommendation_warning = None
    if len(numbers) < 20:
        recommendation_warning = f"目前僅產生 {len(numbers)} 個有效推薦號碼"
    return {
        "target_issue": target_issue,
        "prediction_issue": target_issue,
        "target_issue_source": target_issue_source,
        "based_on_issue": based_on_issue,
        "based_on_draw_time": based_time["based_on_draw_time"],
        "based_on_time_source": based_time["based_on_time_source"],
        "based_on_draw_exists": bool(based_draw),
        "latest_official_issue": current_issue,
        "database_latest_issue": database_latest_issue,
        "expected_target_issue": freshness.get("expected_target_issue") or refresh.get("expected_target_issue"),
        "is_current": status["is_current"],
        "status": freshness["dashboard_status"],
        "target_status": status["status"],
        "stale_status": freshness["stale_status"],
        "stale_status_label": freshness["stale_status_label"],
        "refresh_status": refresh.get("refresh_status"),
        "refresh_reason": refresh.get("refresh_reason"),
        "last_refresh_attempt": refresh.get("last_refresh_attempt"),
        "last_refresh_success": refresh.get("last_refresh_success"),
        "is_stale": freshness["is_stale"],
        "lag_issues": freshness["lag_issues"],
        "expected_draw_time": expected_time,
        "target_draw_time": expected_time,
        "expected_draw_time_source": expected_source,
        "generated_at": record.get("predict_time") or record.get("created_at"),
        "main_numbers": numbers,
        "recommend_numbers": numbers,
        "recommendation_warning": recommendation_warning,
        "backup_numbers": [],
        "super_number": record.get("super_number"),
        "twins": _twins(numbers),
        "consecutive": record.get("consecutive") or _pairs(numbers, 1),
        "patch_numbers": record.get("patch_numbers") or _patch_numbers(numbers),
        "tails": record.get("tails") or _tails(numbers),
        "tail_groups": _tail_groups(numbers),
        "big_small": record.get("big_small") or _big_small(numbers),
        "odd_even": record.get("odd_even") or _odd_even(numbers),
        "confidence": record.get("confidence") or 0,
        "model_version": "V7",
        "release_version": record.get("release_version"),
        "git_commit_hash": record.get("git_commit_hash"),
        "production_generation": record.get("production_generation"),
        "feature_version": record.get("feature_version"),
        "model_scores": record.get("model_scores") or {},
        "winning_model": record.get("winning_model"),
        "source": record.get("source") or "production_history",
        "trigger": record.get("trigger") or "production_read_layer",
        "production_valid": is_production_prediction(record),
        "read_layer": record.get("read_layer") or {},
        "reasons": record.get("reasons") or [],
        "alerts": _alerts(numbers, record.get("super_number")),
        "history": {},
        "laowanjia": {},
    }


def _alert_level(value: int) -> dict:
    value = max(0, min(5, int(value or 0)))
    return {"stars": value, "percent": value * 20}


def _alerts(numbers: list[int], super_number: int | None) -> dict:
    consecutive = len(_pairs(numbers, 1))
    twins = len(_pairs(numbers, 2))
    cluster = max(
        sum(1 for number in numbers if start <= number <= start + 9)
        for start in range(1, 81, 10)
    ) if numbers else 0
    patch = len(_patch_numbers(numbers))
    return {
        "cluster_alert": _alert_level(cluster - 2),
        "patch_alert": _alert_level(patch // 2),
        "twin_alert": _alert_level(twins),
        "consecutive_alert": _alert_level(consecutive),
        "super_alert": _alert_level(3 if super_number else 1),
    }


def _latest_verified_prediction(records: list[dict]) -> dict | None:
    for record in records:
        if record.get("winning_numbers") or record.get("prediction_status") == "verified" or record.get("verified_at"):
            return record
    return None


def _prediction_by_target_issue(target_issue: Any) -> dict | None:
    issue = _valid_production_issue(target_issue)
    if not issue:
        return None
    try:
        from database.prediction_history_store import _prediction_records_for_target_issue

        for record in _prediction_records_for_target_issue(issue):
            if is_production_prediction(record):
                return record
    except Exception:
        logger.exception("dashboard direct prediction lookup failed target_issue=%s", issue)
    return None


def _pending_next_prediction(current_draw: dict | None, detected_latest_issue: Any = None) -> dict:
    database_latest_issue = (current_draw or {}).get("issue")
    current_issue = detected_latest_issue or database_latest_issue
    expected_target = _derive_next_issue(current_issue)
    return {
        "target_issue": expected_target,
        "prediction_issue": expected_target,
        "target_issue_source": "expected_from_latest_issue",
        "based_on_issue": current_issue,
        "latest_official_issue": current_issue,
        "database_latest_issue": database_latest_issue,
        "expected_target_issue": expected_target,
        "is_current": False,
        "status": "prediction_pending",
        "target_status": "pending",
        "stale_status": "prediction_pending",
        "stale_status_label": "prediction_pending",
        "refresh_status": "prediction_pending",
        "refresh_reason": "latest_prediction_missing_or_expired",
        "is_stale": True,
        "lag_issues": None,
        "main_numbers": [],
        "recommend_numbers": [],
        "recommendation_warning": "Latest production prediction is pending for the newest official draw.",
        "production_valid": False,
        "read_layer": {"query_name": "production_latest_prediction_pending", "production_filtered": True},
        "reasons": ["Latest production prediction is pending for the newest official draw."],
        "alerts": {},
        "history": {},
        "laowanjia": {},
    }


def _previous_result_for_based_on(target_issue: Any) -> tuple[dict | None, str]:
    exact = _prediction_by_target_issue(target_issue)
    if exact:
        return exact, "exact_previous"
    fallback = get_latest_verified_prediction_at_or_before(str(target_issue)) if target_issue else None
    if fallback:
        return fallback, "latest_available_verified"
    return None, "unavailable"


def _unavailable_previous_result(requested_target_issue: Any) -> dict:
    return {
        "previous_result_mode": "unavailable",
        "requested_target_issue": requested_target_issue,
        "displayed_target_issue": None,
        "target_issue": None,
        "verified_issue": None,
        "predicted_numbers": [],
        "draw_numbers": [],
        "official_numbers": [],
        "matched_numbers": [],
        "missed_numbers": [],
        "hit_count": 0,
        "prediction_count": 0,
        "hit_denominator": 20,
        "prediction_status": "unavailable",
        "verification_status": "unavailable",
        "learning_used": False,
        "learning_status": "unavailable",
    }


def _verification(record: dict | None, draw: dict | None) -> dict | None:
    if not record:
        return None
    predicted = _as_int_list(record.get("recommend_numbers"))
    draw_numbers = _as_int_list(record.get("winning_numbers") or (draw or {}).get("numbers"))
    matched_set = set(draw_numbers)
    matched = [number for number in predicted if number in matched_set]
    missed = [number for number in predicted if number not in matched_set]
    predicted_super = _as_int(record.get("super_number"))
    actual_super = _as_int((draw or {}).get("super_number"))
    if actual_super is None:
        actual_super = _as_int(record.get("actual_super"))
    draw_time_payload = _based_on_time(record.get("prediction_issue"), draw)
    return {
        "target_issue": record.get("prediction_issue"),
        "prediction_status": record.get("prediction_status"),
        "prediction_created_at": record.get("predict_time") or record.get("created_at"),
        "draw_time": draw_time_payload["based_on_draw_time"] or _format_draw_time(record.get("draw_time")),
        "draw_time_source": draw_time_payload["based_on_time_source"],
        "predicted_numbers": predicted,
        "draw_numbers": draw_numbers,
        "official_numbers": draw_numbers,
        "matched_numbers": matched,
        "missed_numbers": missed,
        "hit_count": len(matched),
        "prediction_count": len(predicted),
        "hit_denominator": len(predicted),
        "hit_label": _hit_label(len(matched)),
        "super_number_predicted": predicted_super,
        "super_number_actual": actual_super,
        "official_super_number": actual_super,
        "super_number_hit": bool(
            predicted_super is not None
            and actual_super is not None
            and predicted_super == actual_super
        ),
        "verified_issue": record.get("verified_issue") or (record.get("prediction_issue") if draw_numbers else None),
        "verified_at": record.get("verified_at") or (record.get("updated_at") if draw_numbers else None),
        "learning_used": bool(record.get("learning_used")),
        "learning_status": "completed" if record.get("learning_used") else "waiting",
        "learned_at": record.get("learned_at"),
        "source": record.get("source") or "production_history",
        "trigger": record.get("trigger") or "production_read_layer",
        "production_valid": is_production_prediction(record),
        "verification_status": "verified" if draw_numbers else "pending",
    }


def _history_item(record: dict) -> dict:
    predicted = _as_int_list(record.get("recommend_numbers"))
    official = _as_int_list(record.get("winning_numbers"))
    matched = _as_int_list(record.get("matched_numbers"))
    if official and not matched:
        official_set = set(official)
        matched = [number for number in predicted if number in official_set]
    missed = _as_int_list(record.get("missed_numbers"))
    if official and not missed:
        official_set = set(official)
        missed = [number for number in predicted if number not in official_set]
    return {
        "id": record.get("id"),
        "issue": record.get("issue"),
        "based_on_issue": record.get("issue"),
        "target_issue": record.get("prediction_issue"),
        "prediction_issue": record.get("prediction_issue"),
        "created_at": record.get("predict_time") or record.get("created_at"),
        "prediction_created_at": record.get("predict_time") or record.get("created_at"),
        "verified_at": record.get("verified_at"),
        "learning_used": bool(record.get("learning_used")),
        "learned_at": record.get("learned_at"),
        "recommend_numbers": predicted,
        "winning_numbers": official,
        "matched_numbers": matched,
        "missed_numbers": missed,
        "hit_count": record.get("hit_count") if record.get("hit_count") is not None else len(matched),
        "prediction_count": record.get("prediction_count") or len(predicted),
        "super_number": record.get("super_number"),
        "official_super_number": record.get("actual_super"),
        "super_number_hit": bool(record.get("super_number_hit") or record.get("super_hit")),
        "prediction_status": record.get("prediction_status"),
        "verification_status": "verified" if official or record.get("prediction_status") == "verified" else "pending",
        "learning_status": "completed" if record.get("learning_used") else "waiting",
        "source": record.get("source") or "production_history",
        "trigger": record.get("trigger") or "production_read_layer",
        "production_valid": is_production_prediction(record),
        "release_version": record.get("release_version"),
        "git_commit_hash": record.get("git_commit_hash"),
        "production_generation": record.get("production_generation"),
        "model_version": record.get("model_version"),
        "feature_version": record.get("feature_version"),
    }


RULE_LIBRARY_NAMES = [
    ("hot", "熱門"),
    ("cold", "冷門"),
    ("missing", "缺號"),
    ("repeat", "重號"),
    ("tail", "尾數"),
    ("gap", "間距"),
    ("cluster", "群聚"),
    ("diagonal", "斜線"),
    ("super", "超級獎"),
    ("laowanjia", "老玩家"),
    ("ladder", "階梯"),
    ("partial_ladder", "偏階"),
    ("extended_ladder", "延階"),
    ("reverse", "反號"),
    ("neighbor", "隔壁號"),
    ("guide", "引路牌"),
    ("integrated", "整合數"),
    ("sunset", "太陽下山"),
    ("momentum", "盤勢動能"),
    ("super_number_trajectory_recovery", "超獎軌跡回補"),
    ("cluster_aftershock_recovery", "群聚後連號回補"),
    ("twins", "雙生"),
    ("consecutive", "連號"),
    ("patch", "補號"),
    ("hot_zone", "熱區"),
    ("cold_zone", "冷區"),
    ("three_star", "三星"),
    ("four_star", "四星"),
    ("five_star", "五星"),
    ("six_star", "六星"),
]


def _flatten_number_groups(groups: Any) -> list[int]:
    values: list[Any] = []
    if isinstance(groups, dict):
        groups = groups.values()
    for item in groups or []:
        if isinstance(item, (list, tuple, set)):
            values.extend(item)
        else:
            values.append(item)
    return _as_int_list(values)


def _rule_item(key: str, label: str, analysis: dict, prediction: dict) -> dict:
    numbers = prediction.get("main_numbers") or []
    status = "ready"
    score = None
    reason = "此項依據目前 prediction snapshot 與最新分析資料產生。"
    impact = "中"
    candidates: list[int] = []

    if key == "hot":
        candidates = _as_int_list(analysis.get("hot_numbers"))[:8]
        reason = "近期熱門號碼與本期候選交集。"
    elif key == "cold":
        candidates = _as_int_list(analysis.get("cold_numbers"))[:8]
        reason = "近期冷門號碼的回補觀察。"
    elif key == "missing":
        candidates = _as_int_list(analysis.get("missing_numbers"))[:8]
        reason = "遺漏較久號碼的回補觀察。"
    elif key == "repeat":
        candidates = _as_int_list(analysis.get("repeated_numbers"))[:8]
        reason = "重號延續觀察。"
    elif key == "tail":
        candidates = numbers
        reason = "依尾數分布檢查候選是否過度集中。"
    elif key == "gap":
        score = analysis.get("gap_score")
        candidates = _flatten_number_groups(analysis.get("difference_values"))[:8]
        reason = "欄位來源為 difference_values / gap_score，正式顯示為間距分析。"
    elif key == "cluster":
        score = analysis.get("cluster_score")
        candidates = numbers
        impact = "高" if str(analysis.get("cluster_level") or "").lower() in ("high", "高") else "中"
        reason = "依十碼區間群聚程度評估。"
    elif key == "diagonal":
        score = analysis.get("diagonal_score")
        candidates = _flatten_number_groups(analysis.get("diagonal_pattern"))[:8]
        reason = "分數來自 diagonal_pattern 命中組數加權，代表斜線型態符合度。"
    elif key == "super":
        super_data = ((analysis.get("ai_score") or {}).get("super_number_trajectory_recovery") or {})
        score = super_data.get("confidence")
        candidates = _as_int_list(super_data.get("candidate_numbers"))[:10]
        reason = "依超級獎軌跡規則產生候選；此指標仍需持續校正。"
    elif key == "super_number_trajectory_recovery":
        super_data = ((analysis.get("ai_score") or {}).get("super_number_trajectory_recovery") or {})
        score = super_data.get("confidence")
        candidates = _as_int_list(super_data.get("candidate_numbers"))[:10]
        reason = "觀察超級獎號近期移動、距離、反轉與可能回補位置。"
    elif key == "cluster_aftershock_recovery":
        cluster_data = ((analysis.get("ai_score") or {}).get("cluster_aftershock_recovery") or {})
        score = cluster_data.get("confidence")
        candidates = _as_int_list(cluster_data.get("candidate_numbers") or cluster_data.get("patch_candidates"))[:10]
        reason = "觀察大群聚後的小連號與回補候選。"
    elif key == "laowanjia":
        score = analysis.get("laowanjia_score")
        candidates = numbers[:8]
        try:
            impact = "高" if float(score or 0) >= 70 else "中"
        except Exception:
            impact = "中"
        reason = "目前盤勢與歷史老玩家規則的符合程度。"
    elif key == "twins":
        candidates = _twins(numbers)
        reason = "候選中出現的雙生號。"
    elif key == "consecutive":
        candidates = sorted({number for pair in _pairs(numbers, 1) for number in pair})
        reason = "候選中出現的連號群。"
    elif key == "patch":
        candidates = _patch_numbers(numbers)
        reason = "依相鄰、間隔與十位補位產生的補號候選。"
    elif key == "hot_zone":
        zones = analysis.get("hot_zone") or []
        candidates = []
        if zones:
            try:
                start = int(zones[0])
                candidates = [number for number in numbers if start <= number <= start + 9]
            except Exception:
                candidates = []
        reason = "近期最明顯的熱區觀察。"
    elif key == "cold_zone":
        candidates = _as_int_list(analysis.get("cold_numbers"))[:8]
        reason = "近期最明顯的冷區觀察。"
    elif key in {"three_star", "four_star", "five_star", "six_star"}:
        candidates = _as_int_list(analysis.get(key))[:10]
        reason = f"{label}候選觀察。"
    else:
        status = "insufficient"
        impact = "資料不足"
        reason = "尚未建立此項分析結果"

    if status == "ready" and not candidates and score is None:
        status = "insufficient"
        impact = "資料不足"
        reason = "資料不足"

    return {
        "key": key,
        "name": label,
        "status": status,
        "score": score,
        "confidence": score,
        "reason": reason,
        "impact": impact,
        "candidate_numbers": candidates,
    }


def _rule_library(analysis: dict | None, prediction: dict) -> dict:
    source = analysis or {}
    rules = [_rule_item(key, label, source, prediction) for key, label in RULE_LIBRARY_NAMES]
    completed = sum(1 for item in rules if item.get("status") == "ready")

    def score_value(item: dict) -> float:
        try:
            return float(item.get("score") or 0)
        except Exception:
            return 0.0

    primary = [
        item["name"]
        for item in sorted(
            rules,
            key=lambda item: (item.get("status") == "ready", score_value(item)),
            reverse=True,
        )
        if item.get("status") == "ready"
    ][:5]
    return {
        "title": "AI 推薦依據",
        "completed_count": completed,
        "total_count": len(RULE_LIBRARY_NAMES),
        "summary": f"本期主要依據：{'、'.join(primary[:3])}" if primary else "尚未建立完整分析摘要",
        "primary_rules": primary,
        "rules": rules,
        "laowanjia_index": source.get("laowanjia_score"),
        "hot_zones": source.get("hot_zone") or [],
        "cold_zone": source.get("cold_zone"),
        "star_prediction": {
            "three_star": source.get("three_star"),
            "four_star": source.get("four_star"),
            "five_star": source.get("five_star"),
            "six_star": source.get("six_star"),
        },
        "super_trajectory": ((source.get("ai_score") or {}).get("super_number_trajectory_recovery") or {}),
        "cluster_recovery": ((source.get("ai_score") or {}).get("cluster_aftershock_recovery") or {}),
    }


def _data_counts(
    history_records: list[dict],
    stats: dict | None = None,
    aggregates: dict | None = None,
) -> dict:
    aggregates = aggregates or {}
    verified_records = [
        item for item in history_records or []
        if item.get("winning_numbers") or item.get("prediction_status") == "verified" or item.get("verified_at")
    ]
    record_count = len(history_records or [])
    return {
        "draw_count": record_count,
        "analysis_count": record_count,
        "prediction_count": aggregates.get("total_prediction_count", record_count),
        "valid_prediction_count": aggregates.get("valid_prediction_count", record_count),
        "valid_target_count": aggregates.get("valid_target_count"),
        "null_target_count": aggregates.get("null_target_count"),
        "has_official_result_count": aggregates.get("has_official_result_count"),
        "verified_prediction_count": aggregates.get("completed_verified_count", (stats or {}).get("verified_prediction_count", len(verified_records))),
        "statistics_sample_count": aggregates.get("valid_sample_count", (stats or {}).get("sample_size", len(verified_records))),
        "learning_sample_count": aggregates.get("learned_distinct_target_count", sum(1 for item in history_records if item.get("learning_used"))),
        "today_draw_count": 0,
        "statistics_scope": "all_history_aggregates",
    }


def _history_stats(history_records: list[dict]) -> dict:
    verified = [
        item for item in history_records or []
        if item.get("winning_numbers") or item.get("prediction_status") == "verified" or item.get("verified_at")
    ]
    total = len(verified)
    if not total:
        return {
            "status": "empty",
            "message": "尚未累積已驗證預測紀錄，系統會持續保存後續推薦。",
            "sample_size": 0,
            "three_star_rate": 0,
            "four_star_rate": 0,
            "five_star_rate": 0,
            "super_hit_rate": 0,
            "average_hits": 0,
            "pending_learning": 0,
            "verified_waiting_learning": 0,
        }
    pending_learning = sum(
        1 for item in verified if not item.get("learning_used")
    )
    return {
        "status": "ok",
        "sample_size": total,
        "three_star_rate": round(sum(1 for item in verified if item.get("three_star_hit")) / total * 100, 2),
        "four_star_rate": round(sum(1 for item in verified if item.get("four_star_hit")) / total * 100, 2),
        "five_star_rate": round(sum(1 for item in verified if (item.get("hit_count") or 0) >= 5) / total * 100, 2),
        "super_hit_rate": round(sum(1 for item in verified if item.get("super_hit") or item.get("super_number_hit")) / total * 100, 2),
        "average_hits": round(sum(item.get("hit_count") or 0 for item in verified) / total, 2),
        "pending_learning": pending_learning,
        "verified_waiting_learning": pending_learning,
    }


def _future_result(name: str, future, warnings: list[str], fallback=None):
    try:
        result = future.result(timeout=PLAYER_DASHBOARD_QUERY_TIMEOUT_SECONDS)
        _store_component_cache(name, result)
        return result
    except TimeoutError:
        logger.warning(
            "player_dashboard_query_timeout component=%s timeout_seconds=%s fallback=last_good_cache",
            name,
            PLAYER_DASHBOARD_QUERY_TIMEOUT_SECONDS,
        )
        warnings.append(f"{name} fallback cache")
        return _load_component_cache(name, fallback)
    except Exception:
        logger.warning(
            "player_dashboard_query_failed component=%s fallback=last_good_cache",
            name,
            exc_info=True,
        )
        warnings.append(f"{name} fallback cache")
        return _load_component_cache(name, fallback)


def build_player_dashboard_summary() -> dict:
    cached = _cached_summary()
    if cached:
        return cached

    warnings: list[str] = []
    generated_at = _now()

    futures = {
        "official_draw": _PLAYER_EXECUTOR.submit(get_latest_official_draw),
        "prediction_history": _PLAYER_EXECUTOR.submit(lambda: get_prediction_history_records(PLAYER_DASHBOARD_HISTORY_LIMIT)),
        "prediction_aggregates": _PLAYER_EXECUTOR.submit(get_prediction_lifecycle_aggregates),
        "analysis": _PLAYER_EXECUTOR.submit(get_latest_analysis_history),
        "kuaishou": _PLAYER_EXECUTOR.submit(get_latest_kuaishou_snapshot),
    }
    official = _future_result("official_draw", futures["official_draw"], warnings)
    history_records = _future_result("prediction_history", futures["prediction_history"], warnings, []) or []
    aggregates = _future_result("prediction_aggregates", futures["prediction_aggregates"], warnings, {}) or {}
    analysis = _future_result("analysis", futures["analysis"], warnings, {}) or {}
    kuaishou = _future_result("kuaishou", futures["kuaishou"], warnings, {}) or {}
    prediction_stats = _history_stats(history_records)
    prediction_stats["history_limit"] = PLAYER_DASHBOARD_HISTORY_LIMIT
    production_history = [_history_item(item) for item in history_records if is_production_prediction(item)]
    active_release = get_current_release()
    production_scope = production_scope_payload()

    current = _current_draw(official)
    latest_prediction = _current_prediction_for_draw(current)
    detected_latest_issue = _max_issue((current or {}).get("issue"), (kuaishou or {}).get("issue"))
    if current and analysis and str(analysis.get("issue") or "") != str(current.get("issue") or ""):
        warnings.append("analysis_pending")
        analysis = {
            "status": "analysis_pending",
            "issue": current.get("issue"),
            "source_issue": current.get("issue"),
            "message": "analysis_history is pending for latest official draw",
        }
    next_prediction = _prediction_from_history(latest_prediction, current, detected_latest_issue) or _pending_next_prediction(current, detected_latest_issue)
    if detected_latest_issue and (current or {}).get("issue") and str(detected_latest_issue) != str((current or {}).get("issue")):
        next_prediction["sync_status"] = "database_behind"
        next_prediction["recommendation_warning"] = (
            f"Production sync stale: database latest issue {(current or {}).get('issue')} "
            f"is behind detected issue {detected_latest_issue}."
        )
    next_prediction["history"] = prediction_stats
    next_prediction["rule_library"] = _rule_library(analysis, next_prediction)
    previous_target_issue = next_prediction.get("based_on_issue")
    verified_record, previous_result_mode = _previous_result_for_based_on(previous_target_issue)
    displayed_target_issue = (verified_record or {}).get("prediction_issue")
    verification_draw = get_official_draw_by_issue(displayed_target_issue) if displayed_target_issue else None
    previous_verification = _verification(verified_record, verification_draw) if verified_record else _unavailable_previous_result(previous_target_issue)
    previous_verification["previous_result_mode"] = previous_result_mode
    previous_verification["requested_target_issue"] = previous_target_issue
    previous_verification["displayed_target_issue"] = displayed_target_issue

    database_issue = (current or {}).get("issue")
    official_issue = detected_latest_issue or (current or {}).get("issue")
    database_int = _as_int(database_issue)
    official_int = _as_int(official_issue)
    lag_count = max((official_int or 0) - (database_int or 0), 0) if database_int and official_int else 0

    payload = {
        "status": "ok",
        "generated_at": generated_at,
        "cache_filter_version": PLAYER_CACHE_FILTER_VERSION,
        "production_filtered": True,
        "production_scope": production_scope,
        "active_release": active_release,
        "release": active_release,
        "current_draw": current,
        "sync": {
            "database_latest_issue": database_issue,
            "official_latest_issue": detected_latest_issue or official_issue,
            "detected_latest_issue": detected_latest_issue,
            "latest_kuaishou_issue": (kuaishou or {}).get("issue"),
            "lag_count": lag_count,
            "is_synced": lag_count == 0 and str(detected_latest_issue or official_issue or "") == str(database_issue or ""),
            "last_successful_collection": (current or {}).get("collected_at"),
            "collection_duration_seconds": None,
        },
        "next_prediction": next_prediction,
        "previous_verification": previous_verification,
        "prediction_history": production_history,
        "data_counts": _data_counts(history_records, prediction_stats, aggregates),
        "history": prediction_stats,
        "aggregates": aggregates,
        "rule_library": next_prediction.get("rule_library"),
        "warnings": warnings,
    }
    _store_summary_cache(payload)
    return payload
