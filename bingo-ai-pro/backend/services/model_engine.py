from __future__ import annotations

from collections import Counter
from typing import Any

from database.analysis_store import get_analysis_history, get_latest_analysis_history
from database.prediction_history_store import get_prediction_history_records

MODEL_NAMES = {
    "laowanjia": "老玩家",
    "hotcold": "HotCold",
    "missing": "Missing",
    "pattern": "Pattern",
    "balance": "Balance",
}


def _as_numbers(values: Any) -> list[int]:
    result = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in result:
            result.append(number)
    return result


def _recent_draws(limit: int = 100) -> list[dict]:
    return get_analysis_history(limit)


def _all_numbers(draws: list[dict]) -> list[int]:
    numbers = []
    for draw in draws:
        numbers.extend(_as_numbers(draw.get("numbers")))
    return numbers


def _confidence(value: float) -> float:
    return round(max(1, min(100, float(value or 0))), 2)


def _top_unique(values: list[int], limit: int = 10) -> list[int]:
    result = []
    for value in values:
        if 1 <= value <= 80 and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def _candidate_from_neighbors(numbers: list[int], limit: int = 10) -> list[int]:
    candidates = []
    for number in numbers:
        for gap in (1, 2, 9, 10, 11):
            for candidate in (number - gap, number + gap):
                if 1 <= candidate <= 80 and candidate not in candidates:
                    candidates.append(candidate)
    return candidates[:limit]


def _model_payload(name: str, candidates: list[int], confidence: float, reason: str) -> dict:
    return {
        "model": name,
        "label": MODEL_NAMES.get(name, name),
        "candidate_numbers": _top_unique(candidates, 10),
        "confidence": _confidence(confidence),
        "reason": reason,
    }


def model_a_laowanjia(draws: list[dict]) -> dict:
    latest = draws[0] if draws else get_latest_analysis_history() or {}
    candidates = []
    for key in ("patch_numbers", "twins", "consecutive", "hot_numbers", "missing_numbers"):
        value = latest.get(key)
        if key in ("twins", "consecutive"):
            for pair in value or []:
                candidates.extend(_as_numbers(pair))
        else:
            candidates.extend(_as_numbers(value))
    score = latest.get("laowanjia_score") or latest.get("cluster_score") or 60
    reason = "老玩家模型依據群聚、雙生、補號、斜線、差值、大小單雙與尾數產生候選。"
    return _model_payload("laowanjia", candidates, score, reason)


def model_b_hotcold(draws: list[dict]) -> dict:
    windows = [draws[:30], draws[:50], draws[:100]]
    scores: Counter[int] = Counter()
    for weight, window in zip((3, 2, 1), windows):
        counter = Counter(_all_numbers(window))
        for number, _ in counter.most_common(12):
            scores[number] += weight
        for number, _ in counter.most_common()[-8:]:
            scores[number] += max(1, weight - 1)
    candidates = [number for number, _ in scores.most_common(10)]
    confidence = 55 + min(40, sum(score for _, score in scores.most_common(10)))
    return _model_payload("hotcold", candidates, confidence, "HotCold 模型綜合近 30/50/100 期熱門與冷門反彈。")


def model_c_missing(draws: list[dict]) -> dict:
    last_seen = {number: None for number in range(1, 81)}
    for index, draw in enumerate(draws):
        for number in _as_numbers(draw.get("numbers")):
            if last_seen[number] is None:
                last_seen[number] = index
    missing_scores = []
    for number, gap in last_seen.items():
        missing_scores.append((number, 999 if gap is None else gap))
    missing_scores.sort(key=lambda item: item[1], reverse=True)
    candidates = [number for number, _ in missing_scores[:10]]
    max_missing = missing_scores[0][1] if missing_scores else 0
    confidence = 50 + min(45, max_missing * 1.5 if max_missing < 999 else 35)
    return _model_payload("missing", candidates, confidence, "Missing 模型依據遺漏值、平均遺漏與最大遺漏挑選補位號。")


def model_d_pattern(draws: list[dict]) -> dict:
    recent = draws[:20]
    pattern_counter = Counter()
    candidates = []
    for draw in recent:
        pattern = str(draw.get("pattern") or draw.get("ai_pattern") or "")
        for label in ("大型群聚", "補號模式", "冷熱交替", "雙生模式", "連號模式"):
            if label in pattern:
                pattern_counter[label] += 1
        candidates.extend(_as_numbers(draw.get("patch_numbers")))
        for pair in draw.get("twins") or []:
            candidates.extend(_as_numbers(pair))
        for run in draw.get("consecutive") or []:
            candidates.extend(_as_numbers(run))
    ranked = [number for number, _ in Counter(candidates).most_common(10)]
    confidence = 55 + min(40, sum(pattern_counter.values()) * 2)
    reason = "Pattern 模型觀察近 20 期大型群聚、補號、冷熱交替、雙生與連號模式。"
    return _model_payload("pattern", ranked, confidence, reason)


def model_e_balance(draws: list[dict]) -> dict:
    recent_numbers = set(_all_numbers(draws[:10]))
    candidates = []
    for zone_start in range(1, 81, 10):
        zone = [number for number in range(zone_start, zone_start + 10) if number not in recent_numbers]
        if zone:
            odds = [number for number in zone if number % 2]
            evens = [number for number in zone if number % 2 == 0]
            if odds:
                candidates.append(odds[len(odds) // 2])
            if evens:
                candidates.append(evens[len(evens) // 2])
    tail_counter = Counter(number % 10 for number in _all_numbers(draws[:30]))
    candidates.sort(key=lambda number: (tail_counter.get(number % 10, 0), number))
    return _model_payload("balance", candidates, 72, "Balance 模型平衡大小、單雙、區間與尾數分布。")


def run_all_models(limit: int = 100) -> dict:
    draws = _recent_draws(limit)
    models = [
        model_a_laowanjia(draws),
        model_b_hotcold(draws),
        model_c_missing(draws),
        model_d_pattern(draws),
        model_e_balance(draws),
    ]
    return {
        "status": "ok" if draws else "warning",
        "source": "analysis_history",
        "latest_issue": draws[0].get("issue") if draws else None,
        "models": models,
    }


def model_hit_rates(limit: int = 100) -> dict:
    records = [item for item in get_prediction_history_records(limit) if item.get("winning_numbers")]
    totals: Counter[str] = Counter()
    hits: Counter[str] = Counter()
    for item in records:
        scores = item.get("model_scores") or {}
        winning = item.get("winning_model")
        for model in scores:
            totals[model] += 1
        if winning:
            hits[winning] += 1
    return {
        model: round((hits[model] / totals[model]) * 100, 2) if totals[model] else 0
        for model in MODEL_NAMES
    }
