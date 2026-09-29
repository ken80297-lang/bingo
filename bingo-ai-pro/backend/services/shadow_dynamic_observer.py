from __future__ import annotations

import atexit
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from statistics import pstdev
from typing import Any

from config.release import GIT_COMMIT_HASH
from database.shadow_dynamic_prediction_store import (
    save_shadow_dynamic_prediction,
    verify_shadow_dynamic_predictions,
)

logger = logging.getLogger(__name__)

ALGORITHM_VERSION = "shadow-dynamic-v1.0"
RULE_KEYS = (
    "composite_market_regime",
    "consecutive_extension",
    "long_dragon",
    "multi_window_hot_cold",
    "neighbor_extension",
    "omission_strength",
    "parity_size_trend",
    "tail_trend_strength",
    "zone_cluster_strength",
)
STRATEGIES = {
    "long_term_lift": "SHADOW_B",
    "long_term_conf_vol": "SHADOW_BC",
}
MAX_SHADOW_BONUS = 70.0

_SHADOW_VERIFY_EXECUTOR: ThreadPoolExecutor | None = None
_SHADOW_VERIFY_EXECUTOR_LOCK = threading.Lock()
_SHADOW_GENERATE_EXECUTOR: ThreadPoolExecutor | None = None
_SHADOW_GENERATE_EXECUTOR_LOCK = threading.Lock()


def _shadow_verify_executor() -> ThreadPoolExecutor:
    global _SHADOW_VERIFY_EXECUTOR
    with _SHADOW_VERIFY_EXECUTOR_LOCK:
        if _SHADOW_VERIFY_EXECUTOR is None:
            _SHADOW_VERIFY_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-verify")
        return _SHADOW_VERIFY_EXECUTOR


def _shadow_generate_executor() -> ThreadPoolExecutor:
    global _SHADOW_GENERATE_EXECUTOR
    with _SHADOW_GENERATE_EXECUTOR_LOCK:
        if _SHADOW_GENERATE_EXECUTOR is None:
            _SHADOW_GENERATE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-generate")
        return _SHADOW_GENERATE_EXECUTOR


def shutdown_shadow_verify_executor() -> None:
    global _SHADOW_VERIFY_EXECUTOR
    with _SHADOW_VERIFY_EXECUTOR_LOCK:
        executor = _SHADOW_VERIFY_EXECUTOR
        _SHADOW_VERIFY_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def shutdown_shadow_generate_executor() -> None:
    global _SHADOW_GENERATE_EXECUTOR
    with _SHADOW_GENERATE_EXECUTOR_LOCK:
        executor = _SHADOW_GENERATE_EXECUTOR
        _SHADOW_GENERATE_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


atexit.register(shutdown_shadow_verify_executor)
atexit.register(shutdown_shadow_generate_executor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not text.isdigit() or text.startswith("99") or text.upper().startswith("TEST"):
        return None
    return text


def _numbers(values: Any) -> list[int]:
    result: list[int] = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in result:
            result.append(number)
    return sorted(result)


def _hit_count(candidates: list[int], actual: list[int]) -> int:
    return len(set(candidates) & set(actual))


def _query_rule_samples(based_on_issue: str, limit: int = 220) -> list[dict]:
    from database.learning_store import get_complete_live_learning_targets, get_learning_records

    by_issue: dict[str, dict] = {}
    cutoff = int(based_on_issue)

    # Production cloud path: one compact set-based query returns exactly the
    # per-issue fields used by shadow metrics. Keep the paged scan below as a
    # fallback for SQLite / transient cloud failures.
    try:
        compact_rows = get_complete_live_learning_targets(limit)
    except Exception:
        logger.exception("shadow compact learning sample query failed; using paged fallback")
        compact_rows = []
    for row in compact_rows:
        issue = _valid_issue(row.get("issue"))
        if not issue or int(issue) > cutoff or issue in by_issue:
            continue
        analysis = row.get("analysis_snapshot") if isinstance(row.get("analysis_snapshot"), dict) else {}
        official = _numbers(row.get("official_numbers"))
        if analysis and len(official) == 20:
            by_issue[issue] = {"issue": issue, "analysis": analysis, "official_numbers": official}
    if len(by_issue) >= limit:
        return [by_issue[key] for key in sorted(by_issue, key=lambda item: int(item))][-limit:]

    offset = 0
    while len(by_issue) < limit and offset < 10000:
        rows = get_learning_records(
            limit=500,
            offset=offset,
            prediction_type="live_prediction",
            verification_status="verified",
            learned_status="learned",
        )
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            issue = _valid_issue(row.get("issue"))
            if not issue or int(issue) > cutoff:
                continue
            if issue in by_issue:
                continue
            analysis = row.get("analysis_snapshot") if isinstance(row.get("analysis_snapshot"), dict) else {}
            official = _numbers(row.get("official_numbers"))
            if not analysis or len(official) != 20:
                continue
            by_issue[issue] = {"issue": issue, "analysis": analysis, "official_numbers": official}
            if len(by_issue) >= limit:
                break
        if len(rows) < 500:
            break
    return [by_issue[key] for key in sorted(by_issue, key=lambda item: int(item))]


def _rule_metrics(samples: list[dict]) -> dict[str, dict]:
    metrics: dict[str, dict] = {}
    for rule_key in RULE_KEYS:
        observations: list[dict] = []
        for sample in samples:
            ai_score = sample.get("analysis", {}).get("ai_score")
            rule_data = ai_score.get(rule_key) if isinstance(ai_score, dict) else None
            candidates = _numbers((rule_data or {}).get("candidate_numbers")) if isinstance(rule_data, dict) else []
            if not candidates:
                continue
            baseline = len(candidates) * 20.0 / 80.0
            hits = _hit_count(candidates, sample.get("official_numbers") or [])
            observations.append(
                {
                    "issue": sample.get("issue"),
                    "candidate_count": len(candidates),
                    "hit_count": hits,
                    "baseline": baseline,
                    "lift": hits - baseline,
                }
            )
        lifts = [float(item["lift"]) for item in observations]
        sample_size = len(observations)
        long_term = sum(lifts) / sample_size if sample_size else 0.0
        recent20_values = lifts[-20:]
        recent20 = sum(recent20_values) / len(recent20_values) if recent20_values else 0.0
        volatility = pstdev(lifts) if len(lifts) > 1 else 0.0
        sample_confidence = min(1.0, math.sqrt(sample_size / 100.0)) if sample_size else 0.0
        stability = 1.0 / (1.0 + volatility)
        recent_guard = 1.0
        if recent20 < 0:
            recent_guard = 0.35
        elif recent20 > 0:
            recent_guard = min(1.15, 0.85 + recent20 / 4.0)
        confidence = sample_confidence * stability * recent_guard
        metrics[rule_key] = {
            "sample_size": sample_size,
            "long_term_lift": round(long_term, 6),
            "recent20_lift": round(recent20, 6),
            "volatility": round(volatility, 6),
            "confidence": round(confidence, 6),
            "average_candidate_count": round(
                sum(float(item["candidate_count"]) for item in observations) / sample_size,
                4,
            )
            if sample_size
            else 0.0,
        }
    return metrics


def _rule_weights(metrics: dict[str, dict], strategy: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for rule_key, item in metrics.items():
        long_term = float(item.get("long_term_lift") or 0)
        if long_term <= 0:
            weights[rule_key] = 0.0
            continue
        if strategy == "long_term_lift":
            weights[rule_key] = round(long_term, 6)
            continue
        confidence = float(item.get("confidence") or 0)
        volatility = float(item.get("volatility") or 0)
        sample_size = int(item.get("sample_size") or 0)
        sample_cap = min(1.0, math.sqrt(sample_size / 100.0))
        volatility_cap = 0.42 / (1.0 + volatility / 1.4)
        raw = long_term * confidence * sample_cap
        weights[rule_key] = round(min(raw, volatility_cap), 6)
    return weights


def _rank_shadow_numbers(
    *,
    production_numbers: list[int],
    analysis: dict,
    weights: dict[str, float],
) -> list[int]:
    production_set = set(production_numbers)
    scores = {number: 0.0 for number in range(1, 81)}
    for rank, number in enumerate(production_numbers):
        scores[number] += 35.0 - rank * 0.5
    total_weight = sum(max(0.0, float(value)) for value in weights.values())
    if total_weight <= 0:
        return sorted(production_numbers[:20])
    ai_score = analysis.get("ai_score") if isinstance(analysis.get("ai_score"), dict) else {}
    for rule_key, raw_weight in weights.items():
        weight = max(0.0, float(raw_weight or 0.0))
        if weight <= 0:
            continue
        normalized = weight / total_weight
        rule_data = ai_score.get(rule_key) if isinstance(ai_score, dict) else {}
        for rank, number in enumerate(_numbers((rule_data or {}).get("candidate_numbers"))[:20]):
            decay = max(0.2, 1.0 - rank * 0.035)
            scores[number] += MAX_SHADOW_BONUS * normalized * decay
            if number not in production_set:
                scores[number] += 2.5 * normalized
    ranked = sorted(scores, key=lambda number: (-scores[number], number))
    return sorted(ranked[:20])


def build_shadow_dynamic_predictions(
    *,
    based_on_issue: str,
    prediction_issue: str,
    production_numbers: list[int],
    analysis: dict,
    generated_at: str | None = None,
) -> list[dict]:
    based_on = _valid_issue(based_on_issue)
    target = _valid_issue(prediction_issue)
    production = _numbers(production_numbers)
    if not based_on or not target or len(production) != 20:
        return []
    analysis_issue = str((analysis or {}).get("issue") or "")
    if analysis_issue != based_on:
        return []
    samples = _query_rule_samples(based_on)
    metrics = _rule_metrics(samples)
    generated = generated_at or _now()
    payloads: list[dict] = []
    for strategy in STRATEGIES:
        weights = _rule_weights(metrics, strategy)
        numbers = _rank_shadow_numbers(production_numbers=production, analysis=analysis, weights=weights)
        payloads.append(
            {
                "based_on_issue": based_on,
                "prediction_issue": target,
                "strategy": strategy,
                "recommend_numbers": numbers,
                "production_numbers": production,
                "rule_weights": weights,
                "rule_metrics": metrics,
                "generated_at": generated,
                "algorithm_version": ALGORITHM_VERSION,
                "git_commit": GIT_COMMIT_HASH,
            }
        )
    return payloads


def generate_for_prediction(recommendation: dict, record: dict) -> dict:
    try:
        based_on = _valid_issue(record.get("issue") or recommendation.get("issue"))
        target = _valid_issue(record.get("prediction_issue") or recommendation.get("target_issue"))
        production_numbers = _numbers(record.get("recommend_numbers"))
        if not based_on or not target or len(production_numbers) != 20:
            return {"status": "skipped", "reason": "invalid_prediction_context"}
        from database.analysis_store import get_latest_analysis_history

        analysis = get_latest_analysis_history() or {}
        payloads = build_shadow_dynamic_predictions(
            based_on_issue=based_on,
            prediction_issue=target,
            production_numbers=production_numbers,
            analysis=analysis,
        )
        saved = [save_shadow_dynamic_prediction(payload) for payload in payloads]
        return {"status": "ok", "count": len(saved), "saved": saved, "algorithm_version": ALGORITHM_VERSION}
    except Exception as exc:
        logger.exception("shadow dynamic observer generation failed")
        return {"status": "error", "message": str(exc)}


def generate_for_prediction_async(recommendation: dict, record: dict) -> dict:
    recommendation_snapshot = dict(recommendation or {})
    record_snapshot = dict(record or {})
    based_on = _valid_issue(record_snapshot.get("issue") or recommendation_snapshot.get("issue"))
    target = _valid_issue(record_snapshot.get("prediction_issue") or recommendation_snapshot.get("target_issue"))
    try:
        _shadow_generate_executor().submit(generate_for_prediction, recommendation_snapshot, record_snapshot)
        return {"status": "queued", "based_on_issue": based_on, "prediction_issue": target, "algorithm_version": ALGORITHM_VERSION}
    except Exception as exc:
        logger.exception("shadow dynamic async generation submit failed target=%s", target)
        return {"status": "error", "based_on_issue": based_on, "prediction_issue": target, "message": str(exc)}


def verify_for_official_draw_async(official_draw: dict) -> dict:
    draw = dict(official_draw or {})
    issue = _valid_issue(draw.get("issue"))
    if not issue or len(_numbers(draw.get("numbers"))) != 20:
        return {"status": "skipped", "reason": "invalid_official_draw", "issue": issue}
    try:
        _shadow_verify_executor().submit(verify_for_official_draw, draw)
        return {"status": "queued", "issue": issue}
    except Exception as exc:
        logger.exception("shadow dynamic async verification submit failed issue=%s", issue)
        return {"status": "error", "issue": issue, "message": str(exc)}


def verify_for_official_draw(official_draw: dict, production_numbers: list[int] | None = None) -> dict:
    issue = _valid_issue((official_draw or {}).get("issue"))
    actual = _numbers((official_draw or {}).get("numbers"))
    if not issue or len(actual) != 20:
        return {"status": "skipped", "reason": "invalid_official_draw", "issue": issue}
    # The production recommendation was frozen into each shadow row at
    # generation time. Verification must use that persisted snapshot instead
    # of rescanning prediction_history.
    return verify_shadow_dynamic_predictions(issue, actual, _numbers(production_numbers))
