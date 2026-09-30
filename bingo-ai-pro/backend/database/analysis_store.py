from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"

_ANALYSIS_HISTORY_CACHE_LOCK = threading.RLock()
_ANALYSIS_HISTORY_CACHE: list[dict] = []
_ANALYSIS_HISTORY_CACHE_MAX = 100

V6_COLUMNS = {
    "cluster_level": ("text", "text"),
    "cluster_score": ("double precision", "real"),
    "twins": ("jsonb", "text"),
    "consecutive": ("jsonb", "text"),
    "three_star": ("jsonb", "text"),
    "four_star": ("jsonb", "text"),
    "five_star": ("jsonb", "text"),
    "six_star": ("jsonb", "text"),
    "diagonal_score": ("double precision", "real"),
    "gap_score": ("double precision", "real"),
    "tail_distribution": ("jsonb", "text"),
    "hot_zone": ("jsonb", "text"),
    "cold_zone": ("jsonb", "text"),
    "patch_numbers": ("jsonb", "text"),
    "pattern": ("text", "text"),
    "ai_pattern": ("text", "text"),
}


def _now() -> str:
    return datetime.utcnow().isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_analysis_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists analysis_history (
                        issue text primary key,
                        draw_time text,
                        numbers jsonb,
                        super_number integer,
                        big_small text,
                        odd_even text,
                        consecutive_numbers jsonb,
                        repeated_numbers jsonb,
                        hot_numbers jsonb,
                        cold_numbers jsonb,
                        missing_numbers jsonb,
                        difference_values jsonb,
                        diagonal_pattern jsonb,
                        laowanjia_score jsonb,
                        ai_score jsonb,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
                for column, (cloud_type, _) in V6_COLUMNS.items():
                    cur.execute(
                        f"alter table analysis_history add column if not exists {column} {cloud_type}"
                    )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud analysis_history table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists analysis_history (
                    issue text primary key,
                    draw_time text,
                    numbers text,
                    super_number integer,
                    big_small text,
                    odd_even text,
                    consecutive_numbers text,
                    repeated_numbers text,
                    hot_numbers text,
                    cold_numbers text,
                    missing_numbers text,
                    difference_values text,
                    diagonal_pattern text,
                    laowanjia_score text,
                    ai_score text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
            existing = {
                row[1]
                for row in conn.execute("pragma table_info(analysis_history)").fetchall()
            }
            for column, (_, sqlite_type) in V6_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"alter table analysis_history add column {column} {sqlite_type}")
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite analysis_history table")

    return results


def _as_int_list(values: Any) -> list[int]:
    result = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80:
            result.append(number)
    return result


def _production_where(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}issue is not null and {prefix}issue not like '99%' and upper({prefix}issue) not like 'TEST%'"


def _issue_number(issue: Any) -> int | None:
    try:
        text = str(issue or "").strip()
        if not text.isdigit():
            return None
        return int(text)
    except Exception:
        return None


def _is_production_draw(draw: dict) -> bool:
    issue = str(draw.get("issue") or "").strip().upper()
    source = str(draw.get("source") or "").strip().lower()
    if not issue or issue.startswith("99") or issue.startswith("TEST"):
        return False
    if "test" in source or "phase" in source:
        return False
    return _issue_number(issue) is not None


def _prior_production_draws(draw: dict, recent: list[dict]) -> list[dict]:
    current_issue = _issue_number(draw.get("issue"))
    prior: list[dict] = []
    for item in recent or []:
        if not isinstance(item, dict) or not _is_production_draw(item):
            continue
        item_issue = _issue_number(item.get("issue"))
        if current_issue is not None and item_issue is not None and item_issue >= current_issue:
            continue
        prior.append(item)
    return prior


def _recent_draws(limit: int = 120) -> list[dict]:
    try:
        from database.official_draw_store import get_official_draw_history

        return get_official_draw_history(limit)
    except Exception:
        logger.exception("failed to load recent draw history for analysis")
        return []


def build_analysis_record(draw: dict, recent_draws: list[dict] | None = None) -> dict:
    numbers = sorted(_as_int_list(draw.get("numbers")))
    recent = _prior_production_draws(draw, recent_draws if recent_draws is not None else _recent_draws())
    previous_numbers = _as_int_list(recent[0].get("numbers")) if recent else []

    all_numbers = []
    for item in recent:
        all_numbers.extend(_as_int_list(item.get("numbers")))
    if not all_numbers:
        all_numbers = numbers

    counter = Counter(all_numbers)
    hot_numbers = [number for number, _ in counter.most_common(10)]
    cold_numbers = [number for number, _ in counter.most_common()[-10:]]

    consecutive_numbers = []
    for number in numbers:
        if number + 1 in numbers:
            consecutive_numbers.append([number, number + 1])

    repeated_numbers = sorted(set(numbers) & set(previous_numbers))
    missing_numbers = [number for number in range(1, 81) if number not in set(all_numbers)][:30]

    difference_values = {}
    for previous in previous_numbers:
        for diff in [1, -1, 9, -9, 10, -10, 11, -11]:
            candidate = previous + diff
            if 1 <= candidate <= 80:
                difference_values.setdefault(str(diff), []).append(candidate)
    difference_values = {
        key: sorted(set(values))
        for key, values in difference_values.items()
    }

    diagonal_pattern = []
    for number in numbers:
        if number + 9 in numbers:
            diagonal_pattern.append([number, number + 9])
        if number + 11 in numbers:
            diagonal_pattern.append([number, number + 11])

    laowanjia_score = {
        "hot": len(set(numbers) & set(hot_numbers)),
        "repeat": len(repeated_numbers),
        "diagonal": len(diagonal_pattern),
        "consecutive": len(consecutive_numbers),
    }
    super_trajectory = _super_number_trajectory(draw, recent)
    cluster_aftershock = _cluster_aftershock(numbers, recent)
    long_dragon = _long_dragon_tracking(draw, recent)
    multi_window_hot_cold = _multi_window_hot_cold(draw, recent)
    omission_strength = _omission_strength(draw, recent)
    neighbor_extension = _neighbor_extension(draw, recent)
    parity_size_trend = _parity_size_trend(draw, recent)
    zone_cluster_strength = _zone_cluster_strength(draw, recent)
    consecutive_extension = _consecutive_extension(draw, recent)
    tail_trend_strength = _tail_trend_strength(draw, recent)
    composite_market_regime = _composite_market_regime({
        "long_dragon": long_dragon,
        "multi_window_hot_cold": multi_window_hot_cold,
        "omission_strength": omission_strength,
        "neighbor_extension": neighbor_extension,
        "parity_size_trend": parity_size_trend,
        "zone_cluster_strength": zone_cluster_strength,
        "consecutive_extension": consecutive_extension,
        "tail_trend_strength": tail_trend_strength,
    })
    ai_score = {
        "score": min(
            100,
            laowanjia_score["hot"] * 5
            + laowanjia_score["repeat"] * 8
            + laowanjia_score["diagonal"] * 6
            + laowanjia_score["consecutive"] * 4,
        ),
        "super_number_trajectory_recovery": super_trajectory,
        "cluster_aftershock_recovery": cluster_aftershock,
        "long_dragon": long_dragon,
        "multi_window_hot_cold": multi_window_hot_cold,
        "omission_strength": omission_strength,
        "neighbor_extension": neighbor_extension,
        "parity_size_trend": parity_size_trend,
        "zone_cluster_strength": zone_cluster_strength,
        "consecutive_extension": consecutive_extension,
        "tail_trend_strength": tail_trend_strength,
        "composite_market_regime": composite_market_regime,
        "learning_features": {
            "trajectory_direction": super_trajectory.get("trend"),
            "trajectory_distance": super_trajectory.get("distance"),
            "trajectory_reversal": super_trajectory.get("trend") == "reversal",
            "trajectory_zone_jump": super_trajectory.get("trend") == "zone_jump",
            "cluster_recovery_age": cluster_aftershock.get("cluster_recovery_age"),
            "cluster_recovery_candidates": cluster_aftershock.get("candidate_numbers"),
            "long_dragon_candidates": long_dragon.get("candidate_numbers"),
            "long_dragon_max_streak": long_dragon.get("max_streak"),
            "long_dragon_active_count": long_dragon.get("active_count"),
            "multi_window_hot_candidates": multi_window_hot_cold.get("candidate_numbers"),
            "multi_window_rising_numbers": multi_window_hot_cold.get("rising_numbers"),
            "multi_window_cooling_numbers": multi_window_hot_cold.get("cooling_numbers"),
            "omission_candidates": omission_strength.get("candidate_numbers"),
            "omission_overdue_numbers": omission_strength.get("overdue_numbers"),
            "omission_recovery_numbers": omission_strength.get("recovery_numbers"),
            "neighbor_extension_candidates": neighbor_extension.get("candidate_numbers"),
            "parity_size_trend_state": parity_size_trend.get("trend_state"),
            "zone_cluster_candidates": zone_cluster_strength.get("candidate_numbers"),
            "consecutive_extension_candidates": consecutive_extension.get("candidate_numbers"),
            "tail_trend_candidates": tail_trend_strength.get("candidate_numbers"),
            "composite_candidates": composite_market_regime.get("candidate_numbers"),
            "composite_consensus": composite_market_regime.get("consensus"),
            "composite_conflicts": composite_market_regime.get("conflicts"),
            "pending_verification_flag": False,
            "source_reliability": "official" if draw.get("source") == "taiwan_lottery" else "collector",
            "data_gap_detected": False,
            "stale_input_flag": False,
        },
    }
    twins = [[number, number + 2] for number in numbers if number + 2 in numbers]
    consecutive = consecutive_numbers
    runs = _runs(numbers)
    three_star = [run[:3] for run in runs if len(run) >= 3]
    four_star = [run[:4] for run in runs if len(run) >= 4]
    five_star = [run[:5] for run in runs if len(run) >= 5]
    six_star = [run[:6] for run in runs if len(run) >= 6]
    decade_counts = {
        f"{start:02d}-{start + 9:02d}": sum(1 for number in numbers if start <= number <= start + 9)
        for start in range(1, 80, 10)
    }
    max_cluster = max(decade_counts.values()) if decade_counts else 0
    cluster_level = "大型群聚" if max_cluster >= 5 else "中型群聚" if max_cluster >= 3 else "小型群聚"
    cluster_score = min(100, max_cluster * 16 + len(consecutive) * 4 + len(twins) * 3)
    tail_distribution = dict(sorted(Counter(number % 10 for number in numbers).items()))
    hot_zone = [zone for zone, count in decade_counts.items() if count >= max_cluster and count > 0]
    min_cluster = min(decade_counts.values()) if decade_counts else 0
    cold_zone = [zone for zone, count in decade_counts.items() if count <= min_cluster]
    patch_numbers = _patch_numbers(numbers)
    diagonal_score = min(100, len(diagonal_pattern) * 12)
    gap_score = min(100, sum(len(values) for values in difference_values.values()) * 2)
    big_small_value = _big_small(numbers)
    odd_even_value = _odd_even(numbers)
    pattern = _pattern(cluster_level, twins, consecutive, patch_numbers)
    laowanjia_total = min(
        100,
        cluster_score * 0.20
        + diagonal_score * 0.18
        + gap_score * 0.12
        + len(twins) * 8
        + len(consecutive) * 6
        + len(patch_numbers) * 2,
    )

    return {
        "issue": str(draw.get("issue")) if draw.get("issue") is not None else None,
        "draw_time": draw.get("draw_time") or draw.get("time_text"),
        "numbers": numbers,
        "super_number": draw.get("super_number"),
        "big_small": draw.get("big_small") or big_small_value,
        "odd_even": draw.get("odd_even") or odd_even_value,
        "consecutive_numbers": consecutive_numbers,
        "repeated_numbers": repeated_numbers,
        "hot_numbers": hot_numbers,
        "cold_numbers": cold_numbers,
        "missing_numbers": missing_numbers,
        "difference_values": difference_values,
        "diagonal_pattern": diagonal_pattern,
        "laowanjia_score": round(laowanjia_total, 2),
        "laowanjia_score_detail": laowanjia_score,
        "ai_score": ai_score,
        "cluster_level": cluster_level,
        "cluster_score": round(cluster_score, 2),
        "twins": twins,
        "consecutive": consecutive,
        "three_star": three_star,
        "four_star": four_star,
        "five_star": five_star,
        "six_star": six_star,
        "diagonal_score": round(diagonal_score, 2),
        "gap_score": round(gap_score, 2),
        "tail_distribution": tail_distribution,
        "hot_zone": hot_zone,
        "cold_zone": cold_zone,
        "patch_numbers": patch_numbers,
        "pattern": pattern,
        "ai_pattern": pattern,
    }


def _runs(numbers: list[int]) -> list[list[int]]:
    number_set = set(numbers)
    runs = []
    for number in numbers:
        if number - 1 in number_set:
            continue
        run = [number]
        current = number
        while current + 1 in number_set:
            current += 1
            run.append(current)
        if len(run) >= 2:
            runs.append(run)
    return runs


def _patch_numbers(numbers: list[int]) -> list[int]:
    result = []
    for number in numbers:
        for gap in (1, 2, 9, 10, 11):
            for candidate in (number - gap, number + gap):
                if 1 <= candidate <= 80 and candidate not in numbers and candidate not in result:
                    result.append(candidate)
    return result[:12]


def _super_number_trajectory(draw: dict, recent: list[dict]) -> dict:
    current = _as_int_list([draw.get("super_number")])
    current_super = current[0] if current else None
    previous = [
        item.get("super_number")
        for item in recent[:10]
        if _as_int_list([item.get("super_number")])
    ]
    previous_numbers = [_as_int_list([value])[0] for value in previous]
    candidate_numbers: list[int] = []
    trend = "stable"
    distance = 0
    if current_super is not None and previous_numbers:
        last = previous_numbers[0]
        distance = current_super - last
        if abs(distance) >= 20:
            trend = "zone_jump"
        elif len(previous_numbers) >= 2 and (current_super - last) * (last - previous_numbers[1]) < 0:
            trend = "reversal"
        elif distance > 0:
            trend = "up"
        elif distance < 0:
            trend = "down"
        for gap in (1, 2, 3, 10, 20):
            for value in (current_super - gap, current_super + gap):
                if 1 <= value <= 80 and value not in candidate_numbers:
                    candidate_numbers.append(value)
    confidence = min(100, 40 + len(candidate_numbers) * 4 + min(abs(distance), 20))
    warning_level = "high" if trend in ("zone_jump", "reversal") else "medium" if abs(distance) >= 10 else "low"
    return {
        "name": "超級獎號軌跡修復",
        "key": "super_number_trajectory_recovery",
        "trend": trend,
        "distance": distance,
        "candidate_numbers": candidate_numbers[:10],
        "reference_issues": [str(item.get("issue")) for item in recent[:10] if item.get("issue")],
        "confidence": round(confidence, 2),
        "triggered_rules": [trend] if current_super is not None else [],
        "warning_level": warning_level,
    }


def _cluster_aftershock(numbers: list[int], recent: list[dict]) -> dict:
    zones = {
        start: sum(1 for number in numbers if start <= number <= start + 9)
        for start in range(1, 80, 10)
    }
    hot_zone = max(zones, key=zones.get) if zones else None
    candidate_numbers: list[int] = []
    if hot_zone is not None:
        for value in range(hot_zone, min(hot_zone + 10, 81)):
            if value not in numbers:
                candidate_numbers.append(value)
    recent_cluster_age = 0
    for index, item in enumerate(recent[:10], start=1):
        item_numbers = _as_int_list(item.get("numbers"))
        if hot_zone and sum(1 for number in item_numbers if hot_zone <= number <= hot_zone + 9) >= 4:
            recent_cluster_age = index
            break
    return {
        "name": "群聚後座力修復",
        "key": "cluster_aftershock_recovery",
        "candidate_numbers": candidate_numbers[:10],
        "cluster_recovery_age": recent_cluster_age,
        "reference_issues": [str(item.get("issue")) for item in recent[:10] if item.get("issue")],
        "confidence": min(100, len(candidate_numbers) * 8 + (20 if recent_cluster_age else 0)),
        "triggered_rules": ["cluster", "recovery"] if candidate_numbers else [],
        "warning_level": "high" if len(candidate_numbers) >= 6 else "medium" if candidate_numbers else "low",
    }


def _composite_market_regime(signals: dict[str, dict]) -> dict:
    votes: Counter = Counter()
    sources: dict[int, list[str]] = {}
    confidence_total = 0.0
    active = 0
    for key, data in signals.items():
        if not isinstance(data, dict):
            continue
        candidates = _as_int_list(data.get("candidate_numbers"))
        if not candidates:
            continue
        active += 1
        confidence = float(data.get("confidence") or 0)
        confidence_total += confidence
        weight = max(0.25, confidence / 100.0)
        for rank, number in enumerate(candidates[:20]):
            rank_weight = max(0.1, 1.0 - rank * 0.04)
            votes[number] += round(weight * rank_weight, 4)
            sources.setdefault(number, []).append(key)
    ranked = sorted(votes, key=lambda number: (-votes[number], -len(set(sources[number])), number))
    consensus = [
        {"number": number, "score": round(votes[number], 4), "sources": sorted(set(sources[number])), "source_count": len(set(sources[number]))}
        for number in ranked[:20]
    ]
    conflicts = [item for item in consensus if item["source_count"] == 1][:10]
    regime = "strong_consensus" if consensus and consensus[0]["source_count"] >= 4 else "mixed" if active >= 3 else "insufficient"
    return {
        "name": "綜合盤勢型態",
        "key": "composite_market_regime",
        "regime": regime,
        "candidate_numbers": [item["number"] for item in consensus],
        "consensus": consensus,
        "conflicts": conflicts,
        "active_signal_count": active,
        "average_signal_confidence": round(confidence_total / active, 2) if active else 0,
        "confidence": min(100, round((consensus[0]["source_count"] / max(1, active)) * 100, 2)) if consensus else 0,
        "shadow_only": True,
    }


def _circular_number(number: int) -> int:
    return ((number - 1) % 80) + 1


def _neighbor_extension(draw: dict, recent: list[dict], *, lookback: int = 20) -> dict:
    current_issue = str(draw.get("issue") or "")
    prior = [item for item in recent if not current_issue or str(item.get("issue") or "") != current_issue][:lookback]
    scores: Counter = Counter()
    evidence: dict[int, list[dict]] = {}
    for age, item in enumerate(prior, start=1):
        weight = max(1, lookback - age + 1)
        for source in set(_as_int_list(item.get("numbers"))):
            for offset in (-2, -1, 1, 2):
                candidate = _circular_number(source + offset)
                scores[candidate] += weight * (2 if abs(offset) == 1 else 1)
                evidence.setdefault(candidate, []).append({"source": source, "offset": offset, "age": age})
    ranked = sorted(scores, key=lambda number: (-scores[number], number))
    return {
        "name": "鄰號延伸",
        "key": "neighbor_extension",
        "candidate_numbers": ranked[:20],
        "scores": {str(number): scores[number] for number in ranked[:20]},
        "evidence": {str(number): evidence[number][:10] for number in ranked[:20]},
        "circular": True,
        "offsets": [-2, -1, 1, 2],
        "confidence": min(100, round(len(prior) / max(1, lookback) * 100, 2)),
        "shadow_only": True,
    }


def _parity_size_trend(draw: dict, recent: list[dict], *, lookback: int = 20) -> dict:
    current_issue = str(draw.get("issue") or "")
    prior = [item for item in recent if not current_issue or str(item.get("issue") or "") != current_issue][:lookback]
    samples = []
    for item in prior:
        nums = _as_int_list(item.get("numbers"))
        if not nums:
            continue
        big = sum(1 for number in nums if number >= 41)
        odd = sum(1 for number in nums if number % 2)
        samples.append({"big": big, "small": len(nums) - big, "odd": odd, "even": len(nums) - odd})
    big_delta = sum(s["big"] - s["small"] for s in samples)
    odd_delta = sum(s["odd"] - s["even"] for s in samples)
    trend_state = {
        "size": "big" if big_delta > 0 else "small" if big_delta < 0 else "balanced",
        "parity": "odd" if odd_delta > 0 else "even" if odd_delta < 0 else "balanced",
    }
    candidates = [
        number for number in range(1, 81)
        if (trend_state["size"] == "balanced" or (number >= 41) == (trend_state["size"] == "big"))
        and (trend_state["parity"] == "balanced" or (number % 2 == 1) == (trend_state["parity"] == "odd"))
    ]
    return {"name": "大小單雙走勢", "key": "parity_size_trend", "trend_state": trend_state, "big_delta": big_delta, "odd_delta": odd_delta,
            "candidate_numbers": candidates[:20], "available_draws": len(samples), "confidence": min(100, len(samples) * 5), "shadow_only": True}


def _zone_cluster_strength(draw: dict, recent: list[dict], *, lookback: int = 20) -> dict:
    current_issue = str(draw.get("issue") or "")
    prior = [item for item in recent if not current_issue or str(item.get("issue") or "") != current_issue][:lookback]
    counts = {start: 0 for start in range(1, 80, 10)}
    for item in prior:
        for number in _as_int_list(item.get("numbers")):
            counts[((number - 1) // 10) * 10 + 1] += 1
    ranked = sorted(counts, key=lambda start: (-counts[start], start))
    hot_starts = ranked[:2]
    candidates = [number for start in hot_starts for number in range(start, min(start + 10, 81))]
    return {"name": "分區群聚強度", "key": "zone_cluster_strength", "zone_counts": {f"{s:02d}-{s+9:02d}": counts[s] for s in counts},
            "hot_zones": [f"{s:02d}-{s+9:02d}" for s in hot_starts], "candidate_numbers": candidates[:20],
            "confidence": min(100, round(len(prior) / max(1, lookback) * 100, 2)), "shadow_only": True}


def _consecutive_extension(draw: dict, recent: list[dict], *, lookback: int = 20) -> dict:
    current_issue = str(draw.get("issue") or "")
    prior = [item for item in recent if not current_issue or str(item.get("issue") or "") != current_issue][:lookback]
    scores: Counter = Counter()
    groups: list[dict] = []
    for age, item in enumerate(prior, start=1):
        nums = sorted(set(_as_int_list(item.get("numbers"))))
        for run in _runs(nums):
            left = _circular_number(run[0] - 1)
            right = _circular_number(run[-1] + 1)
            weight = max(1, lookback - age + 1) * len(run)
            scores[left] += weight
            scores[right] += weight
            groups.append({"run": run, "left": left, "right": right, "age": age})
    ranked = sorted(scores, key=lambda number: (-scores[number], number))
    return {"name": "連號延續", "key": "consecutive_extension", "candidate_numbers": ranked[:20],
            "groups": groups[:30], "scores": {str(n): scores[n] for n in ranked[:20]}, "circular": True,
            "confidence": min(100, len(groups) * 5), "shadow_only": True}


def _tail_trend_strength(draw: dict, recent: list[dict], *, lookback: int = 20) -> dict:
    current_issue = str(draw.get("issue") or "")
    prior = [item for item in recent if not current_issue or str(item.get("issue") or "") != current_issue][:lookback]
    counts = Counter()
    for item in prior:
        counts.update(number % 10 for number in _as_int_list(item.get("numbers")))
    ranked_tails = sorted(range(10), key=lambda tail: (-counts[tail], tail))
    hot_tails = ranked_tails[:3]
    cold_tails = sorted(range(10), key=lambda tail: (counts[tail], tail))[:3]
    candidates = [number for number in range(1, 81) if number % 10 in hot_tails]
    return {"name": "尾數走勢強化", "key": "tail_trend_strength", "hot_tails": hot_tails, "cold_tails": cold_tails,
            "tail_counts": {str(tail): counts[tail] for tail in range(10)}, "candidate_numbers": candidates[:20],
            "confidence": min(100, round(len(prior) / max(1, lookback) * 100, 2)), "shadow_only": True}


def _omission_strength(draw: dict, recent: list[dict], *, lookback: int = 100) -> dict:
    """Measure current, average, and maximum omission gaps without leaking the current draw."""
    current_issue = str(draw.get("issue") or "")
    prior = [item for item in recent if not current_issue or str(item.get("issue") or "") != current_issue][:lookback]
    appearances: dict[int, list[int]] = {number: [] for number in range(1, 81)}
    for index, item in enumerate(prior):
        for number in set(_as_int_list(item.get("numbers"))):
            appearances[number].append(index)

    metrics: list[dict] = []
    for number in range(1, 81):
        positions = appearances[number]
        current_omission = positions[0] if positions else len(prior)
        completed_gaps = [positions[i + 1] - positions[i] - 1 for i in range(len(positions) - 1)]
        if positions:
            completed_gaps.append(max(0, len(prior) - positions[-1] - 1))
        else:
            completed_gaps.append(len(prior))
        average_omission = round(sum(completed_gaps) / len(completed_gaps), 2) if completed_gaps else 0.0
        max_omission = max(completed_gaps + [current_omission], default=current_omission)
        ratio = round(current_omission / max(1.0, average_omission), 3)
        metrics.append({
            "number": number,
            "current_omission": current_omission,
            "average_omission": average_omission,
            "max_omission": max_omission,
            "omission_ratio": ratio,
            "appearance_count": len(positions),
        })

    ranked = sorted(metrics, key=lambda item: (-item["omission_ratio"], -item["current_omission"], item["number"]))
    overdue = [item for item in ranked if item["current_omission"] > item["average_omission"] and item["current_omission"] > 0][:20]
    current_numbers = set(_as_int_list(draw.get("numbers")))
    recovery = [item for item in metrics if item["number"] in current_numbers and item["current_omission"] > 0]
    recovery.sort(key=lambda item: (-item["omission_ratio"], -item["current_omission"], item["number"]))
    candidate_numbers = [item["number"] for item in overdue]
    confidence = min(100, round(len(prior) / max(1, lookback) * 100, 2))
    return {
        "name": "遺漏強度",
        "key": "omission_strength",
        "lookback": lookback,
        "available_draws": len(prior),
        "candidate_numbers": candidate_numbers,
        "overdue_numbers": overdue,
        "recovery_numbers": recovery[:20],
        "metrics": metrics,
        "confidence": confidence,
        "reference_issues": [str(item.get("issue")) for item in prior if item.get("issue")],
        "shadow_only": True,
    }


def _multi_window_hot_cold(draw: dict, recent: list[dict], *, windows: tuple[int, ...] = (10, 20, 50, 100)) -> dict:
    """Build comparable hot/cold rankings across short, medium, and long windows."""
    current_issue = str(draw.get("issue") or "")
    prior = [item for item in recent if not current_issue or str(item.get("issue") or "") != current_issue]
    window_data: dict[str, dict] = {}
    for window in windows:
        sample = prior[:window]
        counts = Counter()
        for item in sample:
            counts.update(set(_as_int_list(item.get("numbers"))))
        ranked_hot = sorted(range(1, 81), key=lambda number: (-counts[number], number))
        ranked_cold = sorted(range(1, 81), key=lambda number: (counts[number], number))
        window_data[str(window)] = {
            "requested_draws": window,
            "available_draws": len(sample),
            "hot_numbers": ranked_hot[:10],
            "cold_numbers": ranked_cold[:10],
            "counts": {str(number): counts[number] for number in range(1, 81)},
        }

    available_windows = [window for window in windows if window_data[str(window)]["available_draws"]]
    short_window = available_windows[0] if available_windows else None
    long_window = available_windows[-1] if available_windows else None
    rising: list[dict] = []
    cooling: list[dict] = []
    if short_window and long_window and short_window != long_window:
        short = window_data[str(short_window)]
        long = window_data[str(long_window)]
        short_draws = max(1, short["available_draws"])
        long_draws = max(1, long["available_draws"])
        for number in range(1, 81):
            short_rate = short["counts"][str(number)] / short_draws
            long_rate = long["counts"][str(number)] / long_draws
            delta = round(short_rate - long_rate, 4)
            item = {"number": number, "short_rate": round(short_rate, 4), "long_rate": round(long_rate, 4), "delta": delta}
            if delta > 0:
                rising.append(item)
            elif delta < 0:
                cooling.append(item)
        rising.sort(key=lambda item: (-item["delta"], item["number"]))
        cooling.sort(key=lambda item: (item["delta"], item["number"]))

    candidate_numbers = [item["number"] for item in rising[:20]]
    coverage = max((window_data[str(window)]["available_draws"] for window in windows), default=0)
    confidence = min(100, round(coverage / max(windows) * 100, 2))
    return {
        "name": "多週期冷熱門",
        "key": "multi_window_hot_cold",
        "windows": window_data,
        "candidate_numbers": candidate_numbers,
        "rising_numbers": rising[:20],
        "cooling_numbers": cooling[:20],
        "short_window": short_window,
        "long_window": long_window,
        "confidence": confidence,
        "reference_issues": [str(item.get("issue")) for item in prior[: max(windows)] if item.get("issue")],
        "shadow_only": True,
    }


def _long_dragon_tracking(draw: dict, recent: list[dict], *, lookback: int = 20) -> dict:
    """Measure consecutive appearance streaks without changing recommendation weights."""
    current_numbers = set(_as_int_list(draw.get("numbers")))
    current_issue = str(draw.get("issue") or "")
    prior = [item for item in recent if not current_issue or str(item.get("issue") or "") != current_issue]
    history = [set(_as_int_list(item.get("numbers"))) for item in prior[:lookback]]
    streaks: list[dict] = []
    for number in sorted(current_numbers):
        streak = 1
        for previous_numbers in history:
            if number not in previous_numbers:
                break
            streak += 1
        if streak >= 2:
            streaks.append({"number": number, "streak": streak})

    streaks.sort(key=lambda item: (-item["streak"], item["number"]))
    candidates = [item["number"] for item in streaks]
    max_streak = max((item["streak"] for item in streaks), default=0)
    strength = min(100, sum(item["streak"] - 1 for item in streaks) * 8 + max(0, max_streak - 2) * 6)
    return {
        "name": "長龍追號",
        "key": "long_dragon",
        "lookback": min(lookback, len(history)),
        "streaks": streaks,
        "candidate_numbers": candidates[:20],
        "max_streak": max_streak,
        "active_count": len(streaks),
        "confidence": round(strength, 2),
        "reference_issues": [str(item.get("issue")) for item in prior[:lookback] if item.get("issue")],
        "triggered_rules": ["consecutive_appearance"] if streaks else [],
        "warning_level": "high" if max_streak >= 4 else "medium" if max_streak >= 3 else "low",
        "shadow_only": True,
    }


def _big_small(numbers: list[int]) -> str:
    big = sum(1 for number in numbers if number >= 41)
    small = len(numbers) - big
    if big > small:
        return "偏大"
    if small > big:
        return "偏小"
    return "均衡"


def _odd_even(numbers: list[int]) -> str:
    odd = sum(1 for number in numbers if number % 2)
    even = len(numbers) - odd
    if odd > even:
        return "偏單"
    if even > odd:
        return "偏雙"
    return "均衡"


def _pattern(cluster_level: str, twins: list, consecutive: list, patch_numbers: list) -> str:
    patterns = [cluster_level]
    if patch_numbers:
        patterns.append("補號模式")
    patterns.append("冷熱交替")
    if twins:
        patterns.append("雙生模式")
    if consecutive:
        patterns.append("連號模式")
    return " / ".join(patterns)


def _save_cloud(record: dict) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into analysis_history
                (
                    issue, draw_time, numbers, super_number, big_small, odd_even,
                    consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
                    missing_numbers, difference_values, diagonal_pattern,
                    laowanjia_score, ai_score,
                    cluster_level, cluster_score, twins, consecutive, three_star,
                    four_star, five_star, six_star, diagonal_score, gap_score,
                    tail_distribution, hot_zone, cold_zone, patch_numbers,
                    pattern, ai_pattern, updated_at
                )
                values (
                    %s, %s, %s::jsonb, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb,
                    %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s, now()
                )
                on conflict (issue) do update set
                    draw_time = excluded.draw_time,
                    numbers = excluded.numbers,
                    super_number = excluded.super_number,
                    big_small = excluded.big_small,
                    odd_even = excluded.odd_even,
                    consecutive_numbers = excluded.consecutive_numbers,
                    repeated_numbers = excluded.repeated_numbers,
                    hot_numbers = excluded.hot_numbers,
                    cold_numbers = excluded.cold_numbers,
                    missing_numbers = excluded.missing_numbers,
                    difference_values = excluded.difference_values,
                    diagonal_pattern = excluded.diagonal_pattern,
                    laowanjia_score = excluded.laowanjia_score,
                    ai_score = excluded.ai_score,
                    cluster_level = excluded.cluster_level,
                    cluster_score = excluded.cluster_score,
                    twins = excluded.twins,
                    consecutive = excluded.consecutive,
                    three_star = excluded.three_star,
                    four_star = excluded.four_star,
                    five_star = excluded.five_star,
                    six_star = excluded.six_star,
                    diagonal_score = excluded.diagonal_score,
                    gap_score = excluded.gap_score,
                    tail_distribution = excluded.tail_distribution,
                    hot_zone = excluded.hot_zone,
                    cold_zone = excluded.cold_zone,
                    patch_numbers = excluded.patch_numbers,
                    pattern = excluded.pattern,
                    ai_pattern = excluded.ai_pattern,
                    updated_at = now()
                """,
                _record_params(record),
            )
        conn.commit()


def _save_sqlite(record: dict) -> None:
    with _sqlite_connection() as conn:
        conn.execute(
            """
            insert into analysis_history
            (
                issue, draw_time, numbers, super_number, big_small, odd_even,
                consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
                missing_numbers, difference_values, diagonal_pattern,
                laowanjia_score, ai_score,
                cluster_level, cluster_score, twins, consecutive, three_star,
                four_star, five_star, six_star, diagonal_score, gap_score,
                tail_distribution, hot_zone, cold_zone, patch_numbers,
                pattern, ai_pattern, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(issue) do update set
                draw_time = excluded.draw_time,
                numbers = excluded.numbers,
                super_number = excluded.super_number,
                big_small = excluded.big_small,
                odd_even = excluded.odd_even,
                consecutive_numbers = excluded.consecutive_numbers,
                repeated_numbers = excluded.repeated_numbers,
                hot_numbers = excluded.hot_numbers,
                cold_numbers = excluded.cold_numbers,
                missing_numbers = excluded.missing_numbers,
                difference_values = excluded.difference_values,
                diagonal_pattern = excluded.diagonal_pattern,
                laowanjia_score = excluded.laowanjia_score,
                ai_score = excluded.ai_score,
                cluster_level = excluded.cluster_level,
                cluster_score = excluded.cluster_score,
                twins = excluded.twins,
                consecutive = excluded.consecutive,
                three_star = excluded.three_star,
                four_star = excluded.four_star,
                five_star = excluded.five_star,
                six_star = excluded.six_star,
                diagonal_score = excluded.diagonal_score,
                gap_score = excluded.gap_score,
                tail_distribution = excluded.tail_distribution,
                hot_zone = excluded.hot_zone,
                cold_zone = excluded.cold_zone,
                patch_numbers = excluded.patch_numbers,
                pattern = excluded.pattern,
                ai_pattern = excluded.ai_pattern,
                updated_at = excluded.updated_at
            """,
            _record_params(record, include_updated_at=True),
        )


def _record_params(record: dict, include_updated_at: bool = False) -> tuple:
    params = (
        record["issue"],
        record.get("draw_time"),
        _json_dumps(record.get("numbers", [])),
        record.get("super_number"),
        record.get("big_small"),
        record.get("odd_even"),
        _json_dumps(record.get("consecutive_numbers", [])),
        _json_dumps(record.get("repeated_numbers", [])),
        _json_dumps(record.get("hot_numbers", [])),
        _json_dumps(record.get("cold_numbers", [])),
        _json_dumps(record.get("missing_numbers", [])),
        _json_dumps(record.get("difference_values", {})),
        _json_dumps(record.get("diagonal_pattern", [])),
        _json_dumps(record.get("laowanjia_score", {})),
        _json_dumps(record.get("ai_score", {})),
        record.get("cluster_level"),
        record.get("cluster_score"),
        _json_dumps(record.get("twins", [])),
        _json_dumps(record.get("consecutive", [])),
        _json_dumps(record.get("three_star", [])),
        _json_dumps(record.get("four_star", [])),
        _json_dumps(record.get("five_star", [])),
        _json_dumps(record.get("six_star", [])),
        record.get("diagonal_score"),
        record.get("gap_score"),
        _json_dumps(record.get("tail_distribution", {})),
        _json_dumps(record.get("hot_zone", [])),
        _json_dumps(record.get("cold_zone", [])),
        _json_dumps(record.get("patch_numbers", [])),
        record.get("pattern"),
        record.get("ai_pattern"),
    )
    if include_updated_at:
        return (*params, _now())
    return params


def _analysis_issue_sort_key(record: dict) -> tuple[int, str]:
    issue = str((record or {}).get("issue") or "")
    try:
        return (1, f"{int(issue):020d}")
    except (TypeError, ValueError):
        return (0, issue)


def _update_analysis_history_cache(record: dict) -> None:
    issue = str((record or {}).get("issue") or "")
    if not issue:
        return
    with _ANALYSIS_HISTORY_CACHE_LOCK:
        retained = [item for item in _ANALYSIS_HISTORY_CACHE if str(item.get("issue") or "") != issue]
        retained.append(dict(record))
        retained.sort(key=_analysis_issue_sort_key, reverse=True)
        _ANALYSIS_HISTORY_CACHE[:] = retained[:_ANALYSIS_HISTORY_CACHE_MAX]


def clear_analysis_history_cache() -> None:
    with _ANALYSIS_HISTORY_CACHE_LOCK:
        _ANALYSIS_HISTORY_CACHE.clear()


def get_cached_analysis_history(limit: int = 100, *, based_on_issue: str | None = None) -> tuple[list[dict], dict]:
    limit = max(1, min(int(limit or 100), _ANALYSIS_HISTORY_CACHE_MAX))
    expected_issue = str(based_on_issue or "")
    with _ANALYSIS_HISTORY_CACHE_LOCK:
        cached = [dict(item) for item in _ANALYSIS_HISTORY_CACHE[:limit]]
    cache_complete = len(cached) >= limit
    cache_current = not expected_issue or any(str(item.get("issue") or "") == expected_issue for item in cached)
    if cache_complete and cache_current:
        return cached, {"source": "memory", "records": len(cached), "based_on_issue": expected_issue or None}

    records = get_analysis_history(limit, use_prediction_pool=True)
    with _ANALYSIS_HISTORY_CACHE_LOCK:
        _ANALYSIS_HISTORY_CACHE[:] = [dict(item) for item in records[:_ANALYSIS_HISTORY_CACHE_MAX]]
    return records, {
        "source": "database",
        "records": len(records),
        "based_on_issue": expected_issue or None,
        "cache_reason": "cold_or_incomplete" if not cache_complete else "stale_based_on_issue",
    }


def save_analysis_history(draw: dict, recent_draws: list[dict] | None = None) -> dict:
    if not draw.get("issue"):
        return {"status": "error", "storage": None, "error": "missing issue"}

    record = build_analysis_record(draw, recent_draws=recent_draws)
    if not record.get("numbers"):
        return {"status": "error", "storage": None, "issue": record.get("issue"), "error": "missing numbers"}

    try:
        _save_cloud(record)
        _update_analysis_history_cache(record)
        return {"status": "ok", "storage": "cloud", "issue": record.get("issue")}
    except Exception as exc:
        logger.exception("cloud analysis_history upsert failed")
        cloud_error = str(exc)

    try:
        _save_sqlite(record)
        # Cloud analysis_history is the production source of truth for the prediction cache.
        # Do not promote SQLite-only degraded writes into the in-memory production snapshot.
        return {
            "status": "ok",
            "storage": "sqlite",
            "issue": record.get("issue"),
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite analysis_history upsert failed")
        return {"status": "error", "storage": None, "issue": record.get("issue"), "error": str(exc)}


def _row_to_record(row: Any) -> dict:
    legacy_laowanjia = _json_loads(row[13])
    laowanjia_value = legacy_laowanjia
    if isinstance(legacy_laowanjia, dict):
        laowanjia_value = legacy_laowanjia.get("score")
        if laowanjia_value is None:
            laowanjia_value = min(
                100,
                (legacy_laowanjia.get("hot") or 0) * 5
                + (legacy_laowanjia.get("repeat") or 0) * 8
                + (legacy_laowanjia.get("diagonal") or 0) * 6
                + (legacy_laowanjia.get("consecutive") or 0) * 4,
            )
    return {
        "issue": row[0],
        "draw_time": row[1],
        "numbers": _json_loads(row[2]) or [],
        "super_number": row[3],
        "big_small": row[4],
        "odd_even": row[5],
        "consecutive_numbers": _json_loads(row[6]) or [],
        "repeated_numbers": _json_loads(row[7]) or [],
        "hot_numbers": _json_loads(row[8]) or [],
        "cold_numbers": _json_loads(row[9]) or [],
        "missing_numbers": _json_loads(row[10]) or [],
        "difference_values": _json_loads(row[11]) or {},
        "diagonal_pattern": _json_loads(row[12]) or [],
        "laowanjia_score": laowanjia_value if laowanjia_value is not None else legacy_laowanjia,
        "laowanjia_score_detail": legacy_laowanjia if isinstance(legacy_laowanjia, dict) else {},
        "ai_score": _json_loads(row[14]) or {},
        "created_at": str(row[15]) if row[15] is not None else None,
        "updated_at": str(row[16]) if row[16] is not None else None,
        "cluster_level": row[17] if len(row) > 17 else None,
        "cluster_score": row[18] if len(row) > 18 else None,
        "twins": _json_loads(row[19]) if len(row) > 19 else [],
        "consecutive": _json_loads(row[20]) if len(row) > 20 else [],
        "three_star": _json_loads(row[21]) if len(row) > 21 else [],
        "four_star": _json_loads(row[22]) if len(row) > 22 else [],
        "five_star": _json_loads(row[23]) if len(row) > 23 else [],
        "six_star": _json_loads(row[24]) if len(row) > 24 else [],
        "diagonal_score": row[25] if len(row) > 25 else None,
        "gap_score": row[26] if len(row) > 26 else None,
        "tail_distribution": _json_loads(row[27]) if len(row) > 27 else {},
        "hot_zone": _json_loads(row[28]) if len(row) > 28 else [],
        "cold_zone": _json_loads(row[29]) if len(row) > 29 else [],
        "patch_numbers": _json_loads(row[30]) if len(row) > 30 else [],
        "pattern": row[32] if len(row) > 32 else None,
        "ai_pattern": row[33] if len(row) > 33 else None,
    }


ANALYSIS_SUMMARY_COLUMNS = (
    "issue",
    "draw_time",
    "numbers",
    "super_number",
    "big_small",
    "odd_even",
    "created_at",
    "updated_at",
    "cluster_level",
    "cluster_score",
    "diagonal_score",
    "gap_score",
    "pattern",
    "ai_pattern",
)
ANALYSIS_SUMMARY_SELECT_COLUMNS = ", ".join(ANALYSIS_SUMMARY_COLUMNS)


def _row_to_summary_record(row: Any) -> dict:
    data = dict(zip(ANALYSIS_SUMMARY_COLUMNS, row))
    return {
        "issue": data.get("issue"),
        "draw_time": data.get("draw_time"),
        "numbers": _json_loads(data.get("numbers")) or [],
        "super_number": data.get("super_number"),
        "big_small": data.get("big_small"),
        "odd_even": data.get("odd_even"),
        "created_at": str(data["created_at"]) if data.get("created_at") is not None else None,
        "updated_at": str(data["updated_at"]) if data.get("updated_at") is not None else None,
        "cluster_level": data.get("cluster_level"),
        "cluster_score": data.get("cluster_score"),
        "diagonal_score": data.get("diagonal_score"),
        "gap_score": data.get("gap_score"),
        "pattern": data.get("pattern"),
        "ai_pattern": data.get("ai_pattern"),
        "consecutive_numbers": [],
        "repeated_numbers": [],
        "hot_numbers": [],
        "cold_numbers": [],
        "missing_numbers": [],
        "difference_values": {},
        "diagonal_pattern": [],
        "laowanjia_score": None,
        "laowanjia_score_detail": {},
        "ai_score": {},
        "twins": [],
        "consecutive": [],
        "three_star": [],
        "four_star": [],
        "five_star": [],
        "six_star": [],
        "tail_distribution": {},
        "hot_zone": [],
        "cold_zone": [],
        "patch_numbers": [],
    }


def _query_cloud(sql: str, params: tuple = ()) -> list[Any]:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _query_sqlite(sql: str, params: tuple = ()) -> list[Any]:
    with _sqlite_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _query_with_fallback(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    try:
        return _query_cloud(sql, params)
    except Exception:
        logger.exception("cloud analysis_history query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite analysis_history query failed")
        return []


def _query_with_fallback_timing(
    sql: str,
    params: tuple = (),
    sqlite_sql: str | None = None,
    *,
    cloud_connection_factory=None,
) -> tuple[list[Any], dict[str, Any]]:
    timing: dict[str, Any] = {
        "query_tag": "analysis_history.by_issue",
        "query_count": 1,
        "backend": "cloud",
    }
    connect_started = time.perf_counter()
    try:
        connection_factory = cloud_connection_factory or _cloud_connection
        with connection_factory() as conn:
            timing["connect_ms"] = round((time.perf_counter() - connect_started) * 1000, 2)
            timing["pool_acquire_ms"] = timing["connect_ms"] if cloud_connection_factory is not None else None
            execute_started = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                timing["execute_ms"] = round((time.perf_counter() - execute_started) * 1000, 2)
                fetch_started = time.perf_counter()
                rows = cur.fetchall()
                timing["fetch_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
                timing["row_count"] = len(rows or [])
                timing["total_ms"] = round((time.perf_counter() - connect_started) * 1000, 2)
                return rows, timing
    except Exception as exc:
        timing["cloud_error_type"] = type(exc).__name__
        logger.exception("cloud analysis_history query failed")

    sqlite_started = time.perf_counter()
    try:
        with _sqlite_connection() as conn:
            timing["backend"] = "sqlite"
            timing["connect_ms"] = round((time.perf_counter() - sqlite_started) * 1000, 2)
            execute_started = time.perf_counter()
            cur = conn.execute(sqlite_sql or sql.replace("%s", "?"), params)
            timing["execute_ms"] = round((time.perf_counter() - execute_started) * 1000, 2)
            fetch_started = time.perf_counter()
            rows = cur.fetchall()
            timing["fetch_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
            timing["row_count"] = len(rows or [])
            timing["total_ms"] = round((time.perf_counter() - sqlite_started) * 1000, 2)
            return rows, timing
    except Exception as exc:
        timing["sqlite_error_type"] = type(exc).__name__
        logger.exception("sqlite analysis_history query failed")
        timing["row_count"] = 0
        timing["total_ms"] = round((time.perf_counter() - sqlite_started) * 1000, 2)
        return [], timing


def get_latest_analysis_history() -> dict | None:
    rows = _query_with_fallback(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit 1
        """,
    )
    return _row_to_record(rows[0]) if rows else None


def get_analysis_history_by_issue(issue: str) -> dict | None:
    rows = _query_with_fallback(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue = %s
        limit 1
        """,
        (str(issue),),
        sqlite_sql="""
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue = ?
        limit 1
        """,
    )
    return _row_to_record(rows[0]) if rows else None


def _dashboard_read_connection():
    from database.postgres import dashboard_read_connection

    return dashboard_read_connection()


def _prediction_read_connection():
    from database.postgres import prediction_lock_connection

    return prediction_lock_connection()


def get_analysis_history_by_issue_with_timing(
    issue: str,
    *,
    use_dashboard_read_pool: bool = False,
) -> tuple[dict | None, dict[str, Any]]:
    cloud_connection_factory = _dashboard_read_connection if use_dashboard_read_pool else None
    rows, timing = _query_with_fallback_timing(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue = %s
        limit 1
        """,
        (str(issue),),
        sqlite_sql="""
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue = ?
        limit 1
        """,
        cloud_connection_factory=cloud_connection_factory,
    )
    transform_started = time.perf_counter()
    record = _row_to_record(rows[0]) if rows else None
    timing["transform_ms"] = round((time.perf_counter() - transform_started) * 1000, 2)
    return record, timing



def get_analysis_history_with_timing(
    limit: int = 100,
    *,
    use_dashboard_read_pool: bool = False,
) -> tuple[list[dict], dict[str, Any]]:
    cloud_connection_factory = _dashboard_read_connection if use_dashboard_read_pool else None
    rows, timing = _query_with_fallback_timing(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit ?
        """,
        cloud_connection_factory=cloud_connection_factory,
    )
    transform_started = time.perf_counter()
    records = [_row_to_record(row) for row in rows]
    timing["transform_ms"] = round((time.perf_counter() - transform_started) * 1000.0, 2)
    timing["total_with_transform_ms"] = round(
        float(timing.get("total_ms") or 0.0) + timing["transform_ms"], 2
    )
    timing["query_tag"] = "analysis_history.recent"
    return records, timing

def get_analysis_history(limit: int = 100, *, use_prediction_pool: bool = False) -> list[dict]:
    rows = _query_with_fallback(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit ?
        """,,
        cloud_connection_factory=_prediction_read_connection if use_prediction_pool else None,
        use_shared_connection=not use_prediction_pool,
    )
    return [_row_to_record(row) for row in rows]


def get_analysis_summary_records(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    rows = _query_with_fallback(
        f"""
        select {ANALYSIS_SUMMARY_SELECT_COLUMNS}
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit %s
        """,
        (limit,),
        sqlite_sql=f"""
        select {ANALYSIS_SUMMARY_SELECT_COLUMNS}
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit ?
        """,
    )
    return [_row_to_summary_record(row) for row in rows]


def get_analysis_statistics(limit: int = 100) -> dict:
    limit = max(1, min(int(limit or 100), 500))
    score_expr = """
        case
            when jsonb_typeof(laowanjia_score) = 'number'
                then (laowanjia_score #>> '{}')::double precision
            when jsonb_typeof(laowanjia_score) = 'object' and laowanjia_score ? 'score'
                then nullif(laowanjia_score->>'score', '')::double precision
            else null
        end
    """
    sqlite_score_expr = """
        case
            when json_valid(laowanjia_score) and json_type(laowanjia_score, '$.score') in ('integer', 'real')
                then cast(json_extract(laowanjia_score, '$.score') as real)
            when json_valid(laowanjia_score) and json_type(laowanjia_score) in ('integer', 'real')
                then cast(laowanjia_score as real)
            else null
        end
    """
    rows = _query_with_fallback(
        f"""
        with recent as (
            select issue, created_at, updated_at, cluster_level, {score_expr} as score
            from analysis_history
            where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
              and cluster_level is not null
            order by issue desc
            limit %s
        )
        select count(*) as analysis_count,
               max(issue) as latest_issue,
               max(coalesce(updated_at, created_at)) as last_analysis_time,
               avg(score) as average_laowanjia_score
        from recent
        """,
        (limit,),
        sqlite_sql=f"""
        with recent as (
            select issue, created_at, updated_at, cluster_level, {sqlite_score_expr} as score
            from analysis_history
            where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
              and cluster_level is not null
            order by issue desc
            limit ?
        )
        select count(*) as analysis_count,
               max(issue) as latest_issue,
               max(coalesce(updated_at, created_at)) as last_analysis_time,
               avg(score) as average_laowanjia_score
        from recent
        """,
    )
    if not rows or not int(rows[0][0] or 0):
        return {
            "status": "empty",
            "analysis_count": 0,
            "latest_issue": None,
            "last_analysis_time": None,
            "average_laowanjia_score": 0,
            "cluster_distribution": {},
        }
    cluster_rows = _query_with_fallback(
        """
        with recent as (
            select cluster_level
            from analysis_history
            where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
              and cluster_level is not null
            order by issue desc
            limit %s
        )
        select coalesce(cluster_level, %s) as cluster_level, count(*) as count
        from recent
        group by coalesce(cluster_level, %s)
        """,
        (limit, "unknown", "unknown"),
        sqlite_sql="""
        with recent as (
            select cluster_level
            from analysis_history
            where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
              and cluster_level is not null
            order by issue desc
            limit ?
        )
        select coalesce(cluster_level, ?) as cluster_level, count(*) as count
        from recent
        group by coalesce(cluster_level, ?)
        """,
    )
    row = rows[0]
    return {
        "status": "ok",
        "analysis_count": int(row[0] or 0),
        "latest_issue": row[1],
        "last_analysis_time": str(row[2]) if row[2] is not None else None,
        "average_laowanjia_score": round(float(row[3] or 0), 2),
        "cluster_distribution": {str(item[0]): int(item[1] or 0) for item in cluster_rows},
    }
