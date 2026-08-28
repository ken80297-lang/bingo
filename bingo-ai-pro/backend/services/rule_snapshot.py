from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from database.analysis_store import get_analysis_history, get_analysis_history_by_issue
from database.prediction_history_store import get_prediction_for_source_target, get_prediction_history_records
from database.rule_snapshot_store import get_rule_snapshots, save_rule_snapshot

try:
    from config.release import RULE_LIBRARY_VERSION
except Exception:  # pragma: no cover - release config can be unavailable in isolated imports.
    RULE_LIBRARY_VERSION = "unknown"


@dataclass(frozen=True)
class RuleDefinition:
    key: str
    label: str
    family: str
    source_fields: tuple[str, ...]
    experimental: bool = False


RULE_REGISTRY: tuple[RuleDefinition, ...] = (
    RuleDefinition("hot", "熱門", "trend", ("hot_numbers",)),
    RuleDefinition("cold", "冷門", "trend", ("cold_numbers",)),
    RuleDefinition("missing", "缺號", "trend", ("missing_numbers",)),
    RuleDefinition("repeat", "重號", "trend", ("repeated_numbers",)),
    RuleDefinition("tail", "尾數", "shape", ("tail_distribution",)),
    RuleDefinition("gap", "間距", "gap", ("difference_values", "gap_score")),
    RuleDefinition("cluster", "群聚", "zone", ("cluster_level", "cluster_score")),
    RuleDefinition("diagonal", "斜線", "shape", ("diagonal_pattern", "diagonal_score")),
    RuleDefinition("super", "超級獎", "special", ("ai_score",)),
    RuleDefinition("laowanjia", "老玩家", "legacy", ("laowanjia_score", "laowanjia_score_detail")),
    RuleDefinition("ladder", "階梯", "dashboard", (), experimental=True),
    RuleDefinition("partial_ladder", "偏階", "dashboard", (), experimental=True),
    RuleDefinition("extended_ladder", "延階", "dashboard", (), experimental=True),
    RuleDefinition("reverse", "反號", "dashboard", (), experimental=True),
    RuleDefinition("neighbor", "隔壁號", "dashboard", (), experimental=True),
    RuleDefinition("guide", "引路牌", "dashboard", (), experimental=True),
    RuleDefinition("integrated", "整合數", "dashboard", (), experimental=True),
    RuleDefinition("sunset", "太陽下山", "dashboard", (), experimental=True),
    RuleDefinition("momentum", "盤勢動能", "dashboard", (), experimental=True),
    RuleDefinition("super_number_trajectory_recovery", "超獎軌跡回補", "special", ("ai_score",)),
    RuleDefinition("cluster_aftershock_recovery", "群聚後連號回補", "special", ("ai_score",)),
    RuleDefinition("twins", "雙生", "shape", ("twins",)),
    RuleDefinition("consecutive", "連號", "shape", ("consecutive", "consecutive_numbers")),
    RuleDefinition("patch", "補號", "gap", ("patch_numbers",)),
    RuleDefinition("hot_zone", "熱區", "zone", ("hot_zone",)),
    RuleDefinition("cold_zone", "冷區", "zone", ("cold_zone",)),
    RuleDefinition("three_star", "三星", "shape", ("three_star",)),
    RuleDefinition("four_star", "四星", "shape", ("four_star",)),
    RuleDefinition("five_star", "五星", "shape", ("five_star",)),
    RuleDefinition("six_star", "六星", "shape", ("six_star",)),
)


def get_rule_registry() -> list[dict]:
    return [
        {
            "key": item.key,
            "label": item.label,
            "family": item.family,
            "source_fields": list(item.source_fields),
            "experimental": item.experimental,
        }
        for item in RULE_REGISTRY
    ]


def generate_rule_snapshot_for_issue(
    source_issue: str,
    target_issue: str | None = None,
    *,
    persist: bool = True,
) -> dict:
    resolved_source_issue = _string_or_none(source_issue)
    resolved_target_issue = _string_or_none(target_issue)
    if not resolved_source_issue:
        return {
            "status": "error",
            "reason": "missing_source_issue",
            "source_issue": resolved_source_issue,
            "target_issue": resolved_target_issue,
            "snapshot": None,
            "persisted": False,
            "saved": {"status": "skipped", "reason": "missing_source_issue"},
        }

    analysis = get_analysis_history_by_issue(resolved_source_issue)
    if not analysis:
        return {
            "status": "skipped",
            "reason": "analysis_not_found",
            "source_issue": resolved_source_issue,
            "target_issue": resolved_target_issue,
            "snapshot": None,
            "persisted": False,
            "saved": {"status": "skipped", "reason": "analysis_not_found"},
        }

    prediction = None
    if resolved_target_issue:
        prediction = get_prediction_for_source_target(resolved_source_issue, resolved_target_issue)

    snapshot = build_rule_snapshot(
        analysis,
        prediction or {},
        source_issue=resolved_source_issue,
        target_issue=resolved_target_issue,
    )
    if not persist:
        return {
            "status": "ok",
            "source_issue": resolved_source_issue,
            "target_issue": resolved_target_issue,
            "snapshot": snapshot,
            "prediction_found": bool(prediction),
            "persisted": False,
            "saved": {"status": "skipped", "reason": "persist_false"},
        }

    saved = save_rule_snapshot(snapshot)
    return {
        "status": "ok" if saved.get("status") == "ok" else "error",
        "source_issue": resolved_source_issue,
        "target_issue": resolved_target_issue,
        "snapshot": snapshot,
        "prediction_found": bool(prediction),
        "persisted": saved.get("status") == "ok",
        "saved": saved,
    }


def build_rule_snapshot_health(limit: int = 100) -> dict:
    limit = max(1, min(int(limit or 100), 1000))
    warnings: list[str] = []
    analysis_records = get_analysis_history(limit)
    snapshot_records = get_rule_snapshots(limit)
    try:
        prediction_records = get_prediction_history_records(limit)
    except Exception:
        prediction_records = []
        warnings.append("prediction_history_unavailable")

    analysis_issues = [
        _string_or_none(item.get("issue"))
        for item in analysis_records
        if _string_or_none(item.get("issue"))
    ]
    analysis_issue_set = set(analysis_issues)
    snapshots = [_snapshot_payload(record) for record in snapshot_records]
    snapshot_source_issues = {
        _string_or_none(snapshot.get("source_issue"))
        for snapshot in snapshots
        if _string_or_none(snapshot.get("source_issue"))
    }
    missing_snapshot_issues = [
        issue for issue in analysis_issues if issue not in snapshot_source_issues
    ]
    coverage_rate = round(
        (len(analysis_issue_set & snapshot_source_issues) / len(analysis_issue_set)) * 100,
        2,
    ) if analysis_issue_set else 0

    prediction_targets_by_source = {
        _string_or_none(item.get("issue")): _string_or_none(item.get("prediction_issue") or item.get("target_issue"))
        for item in prediction_records
        if _string_or_none(item.get("issue"))
    }
    stale_snapshot_issues: list[str] = []
    incomplete_snapshot_count = 0
    recommendation_ready_count = 0
    dashboard_ready_count = 0
    versions: set[str] = set()

    for snapshot in snapshots:
        source_issue = _string_or_none(snapshot.get("source_issue"))
        target_issue = _string_or_none(snapshot.get("target_issue"))
        version = _string_or_none(snapshot.get("rule_library_version"))
        if version:
            versions.add(version)
        if _is_incomplete_snapshot(snapshot):
            incomplete_snapshot_count += 1
        if _recommendation_ready(snapshot):
            recommendation_ready_count += 1
        if _dashboard_ready(snapshot):
            dashboard_ready_count += 1
        expected_target = prediction_targets_by_source.get(source_issue)
        if (
            source_issue
            and (
                source_issue not in analysis_issue_set
                or (expected_target and target_issue and target_issue != expected_target)
            )
            and source_issue not in stale_snapshot_issues
        ):
            stale_snapshot_issues.append(source_issue)

    if not snapshot_records:
        warnings.append("no_rule_snapshots")
    if missing_snapshot_issues:
        warnings.append("missing_rule_snapshots")
    if stale_snapshot_issues:
        warnings.append("stale_rule_snapshots")
    if incomplete_snapshot_count:
        warnings.append("incomplete_rule_snapshots")
    if len(versions) > 1:
        warnings.append("mixed_rule_library_versions")

    status = "ok"
    if analysis_records and coverage_rate < 100:
        status = "warning"
    if not snapshot_records or (analysis_records and coverage_rate == 0):
        status = "critical"
    if incomplete_snapshot_count and status == "ok":
        status = "warning"

    return {
        "status": status,
        "analysis_count": len(analysis_records),
        "snapshot_count": len(snapshot_records),
        "coverage_rate": coverage_rate,
        "latest_analysis_issue": analysis_issues[0] if analysis_issues else None,
        "latest_snapshot_source_issue": _string_or_none(snapshots[0].get("source_issue")) if snapshots else None,
        "missing_snapshot_issues": missing_snapshot_issues,
        "stale_snapshot_issues": stale_snapshot_issues,
        "rule_library_versions": sorted(versions),
        "incomplete_snapshot_count": incomplete_snapshot_count,
        "recommendation_ready_count": recommendation_ready_count,
        "dashboard_ready_count": dashboard_ready_count,
        "warnings": warnings,
    }


def build_rule_snapshot(
    analysis: dict | None,
    prediction: dict | None = None,
    *,
    source_issue: str | None = None,
    target_issue: str | None = None,
    generated_at: str | None = None,
    rule_library_version: str | None = None,
) -> dict:
    analysis = analysis or {}
    prediction = prediction or {}
    resolved_source_issue = source_issue or _string_or_none(
        analysis.get("issue") or prediction.get("issue") or prediction.get("based_on_issue")
    )
    resolved_target_issue = target_issue or _string_or_none(
        prediction.get("prediction_issue") or prediction.get("target_issue")
    )
    rules = [_build_rule_item(rule, analysis, prediction) for rule in RULE_REGISTRY]
    ready_rules = [item for item in rules if item.get("status") == "ready"]
    primary = sorted(
        ready_rules,
        key=lambda item: (
            _number_or_zero(item.get("score")),
            len(item.get("candidate_numbers") or []),
        ),
        reverse=True,
    )[:5]
    return {
        "source_issue": resolved_source_issue,
        "target_issue": resolved_target_issue,
        "rule_library_version": rule_library_version or RULE_LIBRARY_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "input_refs": {
            "analysis_issue": _string_or_none(analysis.get("issue")),
            "prediction_issue": _string_or_none(
                prediction.get("prediction_issue") or prediction.get("target_issue")
            ),
            "history_cutoff_issue": resolved_source_issue,
        },
        "fast_path_sources": _build_fast_path_sources(analysis),
        "rules": rules,
        "aggregate": {
            "completed_count": len(ready_rules),
            "total_count": len(rules),
            "primary_rules": [item["key"] for item in primary],
        },
    }


def _build_fast_path_sources(analysis: dict) -> dict[str, list[int]]:
    return {
        "patch_numbers": _numbers(analysis.get("patch_numbers")),
        "missing_numbers": _numbers(analysis.get("missing_numbers")),
        "cold_numbers": _numbers(analysis.get("cold_numbers")),
        "hot_numbers": _numbers(analysis.get("hot_numbers")),
        "diagonal_pattern": _flatten(_groups(analysis.get("diagonal_pattern"))),
        "repeated_numbers": _numbers(analysis.get("repeated_numbers")),
        "latest_draw_numbers": _numbers(analysis.get("numbers")),
    }


def _build_rule_item(rule: RuleDefinition, analysis: dict, prediction: dict) -> dict:
    score: float | int | None = None
    confidence: float | int | None = None
    candidates: list[int] = []
    candidate_groups: list[Any] = []
    warnings: list[str] = []

    if rule.experimental:
        status = "experimental"
        reason = "此規則已列入 registry，但尚未接上正式 executor。"
    else:
        status = "ready"
        reason = "由既有 analysis_history 欄位轉成標準化規則快照。"

    if rule.key == "hot":
        candidates = _numbers(analysis.get("hot_numbers"))[:10]
    elif rule.key == "cold":
        candidates = _numbers(analysis.get("cold_numbers"))[:10]
    elif rule.key == "missing":
        candidates = _numbers(analysis.get("missing_numbers"))[:12]
    elif rule.key == "repeat":
        candidates = _numbers(analysis.get("repeated_numbers"))[:10]
    elif rule.key == "tail":
        candidate_groups = _tail_groups(analysis.get("tail_distribution"), prediction)
    elif rule.key == "gap":
        score = analysis.get("gap_score")
        candidate_groups = _groups(analysis.get("difference_values"))
        candidates = _flatten(candidate_groups)[:12]
    elif rule.key == "cluster":
        score = analysis.get("cluster_score")
        candidate_groups = [{"level": analysis.get("cluster_level")}]
        candidates = _numbers(prediction.get("main_numbers") or prediction.get("recommend_numbers"))[:20]
    elif rule.key == "diagonal":
        score = analysis.get("diagonal_score")
        candidate_groups = _groups(analysis.get("diagonal_pattern"))
        candidates = _flatten(candidate_groups)[:12]
    elif rule.key in {"super", "super_number_trajectory_recovery"}:
        data = _nested_rule(analysis, "super_number_trajectory_recovery")
        score = data.get("confidence")
        confidence = data.get("confidence")
        candidates = _numbers(data.get("candidate_numbers"))[:10]
        candidate_groups = [data] if data else []
    elif rule.key == "cluster_aftershock_recovery":
        data = _nested_rule(analysis, "cluster_aftershock_recovery")
        score = data.get("confidence")
        confidence = data.get("confidence")
        candidates = _numbers(data.get("candidate_numbers") or data.get("patch_candidates"))[:10]
        candidate_groups = [data] if data else []
    elif rule.key == "laowanjia":
        score = analysis.get("laowanjia_score")
        confidence = analysis.get("laowanjia_score")
        candidate_groups = [_compact_dict(analysis.get("laowanjia_score_detail"))]
    elif rule.key == "twins":
        candidate_groups = _groups(analysis.get("twins"))
        candidates = _flatten(candidate_groups)[:12]
    elif rule.key == "consecutive":
        candidate_groups = _groups(analysis.get("consecutive") or analysis.get("consecutive_numbers"))
        candidates = _flatten(candidate_groups)[:12]
    elif rule.key == "patch":
        candidates = _numbers(analysis.get("patch_numbers"))[:12]
    elif rule.key == "hot_zone":
        candidate_groups = _zone_groups(analysis.get("hot_zone"))
        candidates = _numbers_in_zones(prediction, analysis.get("hot_zone"))
    elif rule.key == "cold_zone":
        candidate_groups = _zone_groups(analysis.get("cold_zone"))
        candidates = _numbers_in_zones(prediction, analysis.get("cold_zone")) or _numbers(
            analysis.get("cold_numbers")
        )[:10]
    elif rule.key in {"three_star", "four_star", "five_star", "six_star"}:
        candidate_groups = _groups(analysis.get(rule.key))
        candidates = _flatten(candidate_groups)[:12]

    if not rule.experimental and not candidates and not candidate_groups and score is None:
        status = "insufficient"
        reason = "既有資料不足，尚無法產生此規則快照。"

    if rule.key == "super":
        warnings.append("super 是 super_number_trajectory_recovery 的相容別名，建議後續合併。")
    if rule.key == "twins":
        warnings.append("analysis twins 目前代表差 2 組合，Dashboard helper 曾使用重複數字定義。")

    return {
        "key": rule.key,
        "label": rule.label,
        "family": rule.family,
        "status": status,
        "score": _number_or_none(score),
        "confidence": _number_or_none(confidence if confidence is not None else score),
        "candidate_numbers": candidates,
        "candidate_groups": candidate_groups,
        "source_fields": list(rule.source_fields),
        "reason": reason,
        "warnings": warnings,
        "version": RULE_LIBRARY_VERSION,
    }


def _numbers(values: Any) -> list[int]:
    result: list[int] = []
    for value in _iter_values(values):
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in result:
            result.append(number)
    return result


def _iter_values(values: Any) -> list[Any]:
    if values is None:
        return []
    if isinstance(values, dict):
        return list(values.values())
    if isinstance(values, (list, tuple, set)):
        result: list[Any] = []
        for item in values:
            if isinstance(item, (list, tuple, set)):
                result.extend(item)
            else:
                result.append(item)
        return result
    return [values]


def _groups(values: Any) -> list[Any]:
    if values is None:
        return []
    if isinstance(values, dict):
        return [
            {"key": str(key), "numbers": _numbers(value)}
            for key, value in values.items()
        ]
    if isinstance(values, list):
        return values
    return [values]


def _flatten(groups: list[Any]) -> list[int]:
    values: list[Any] = []
    for item in groups:
        if isinstance(item, dict):
            values.extend(item.get("numbers") or item.get("candidate_numbers") or [])
        elif isinstance(item, (list, tuple, set)):
            values.extend(item)
        else:
            values.append(item)
    return _numbers(values)


def _nested_rule(analysis: dict, key: str) -> dict:
    ai_score = analysis.get("ai_score") if isinstance(analysis.get("ai_score"), dict) else {}
    data = ai_score.get(key)
    return data if isinstance(data, dict) else {}


def _tail_groups(distribution: Any, prediction: dict) -> list[dict]:
    if not isinstance(distribution, dict):
        return []
    prediction_numbers = _numbers(prediction.get("main_numbers") or prediction.get("recommend_numbers"))
    groups = []
    for tail, count in sorted(distribution.items(), key=lambda item: str(item[0])):
        try:
            tail_number = int(tail)
        except Exception:
            continue
        groups.append(
            {
                "tail": tail_number,
                "count": count,
                "numbers": [number for number in prediction_numbers if number % 10 == tail_number],
            }
        )
    return groups


def _zone_groups(zones: Any) -> list[dict]:
    groups = []
    for zone in zones or []:
        bounds = _zone_bounds(zone)
        groups.append({"zone": zone, "start": bounds[0], "end": bounds[1]})
    return groups


def _numbers_in_zones(prediction: dict, zones: Any) -> list[int]:
    prediction_numbers = _numbers(prediction.get("main_numbers") or prediction.get("recommend_numbers"))
    result: list[int] = []
    for zone in zones or []:
        start, end = _zone_bounds(zone)
        if start is None or end is None:
            continue
        for number in prediction_numbers:
            if start <= number <= end and number not in result:
                result.append(number)
    return result


def _zone_bounds(zone: Any) -> tuple[int | None, int | None]:
    if isinstance(zone, int):
        return zone, min(zone + 9, 80)
    text = str(zone)
    if "-" in text:
        start, end = text.split("-", 1)
        try:
            return int(start), int(end)
        except Exception:
            return None, None
    try:
        start = int(text)
    except Exception:
        return None, None
    return start, min(start + 9, 80)


def _compact_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _number_or_none(value: Any) -> float | int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return int(number) if number.is_integer() else number


def _number_or_zero(value: Any) -> float:
    number = _number_or_none(value)
    return float(number or 0)


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _snapshot_payload(record: dict) -> dict:
    record = record if isinstance(record, dict) else {}
    snapshot = record.get("snapshot_json")
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        **snapshot,
        "source_issue": _string_or_none(snapshot.get("source_issue") or record.get("source_issue")),
        "target_issue": _string_or_none(snapshot.get("target_issue") or record.get("target_issue")),
        "rule_library_version": _string_or_none(
            snapshot.get("rule_library_version") or record.get("rule_library_version")
        ),
    }


def _is_incomplete_snapshot(snapshot: dict) -> bool:
    rules = snapshot.get("rules")
    aggregate = snapshot.get("aggregate") if isinstance(snapshot.get("aggregate"), dict) else {}
    total_count = aggregate.get("total_count")
    if not snapshot.get("source_issue") or not snapshot.get("rule_library_version"):
        return True
    if not isinstance(rules, list) or not rules:
        return True
    try:
        return int(total_count) != len(rules)
    except Exception:
        return True


_FAST_PATH_REQUIRED_SOURCE_KEYS = (
    "patch_numbers",
    "missing_numbers",
    "cold_numbers",
    "hot_numbers",
    "diagonal_pattern",
    "repeated_numbers",
)


def _snapshot_source_numbers(value: Any) -> list[int]:
    direct = _numbers(value)
    if direct:
        return direct
    return _flatten(_groups(value))


def _recommendation_ready(snapshot: dict) -> bool:
    if not isinstance(snapshot, dict):
        return False
    fast_path_sources = snapshot.get("fast_path_sources")
    if isinstance(fast_path_sources, dict):
        return all(
            key in fast_path_sources if key == "repeated_numbers" else bool(_snapshot_source_numbers(fast_path_sources.get(key)))
            for key in _FAST_PATH_REQUIRED_SOURCE_KEYS
        )

    rules = snapshot.get("rules")
    if not isinstance(rules, list):
        return False
    rules_by_key = {
        str(item.get("key")): item
        for item in rules
        if isinstance(item, dict) and item.get("key")
    }
    key_map = {
        "patch_numbers": "patch",
        "missing_numbers": "missing",
        "cold_numbers": "cold",
        "hot_numbers": "hot",
        "diagonal_pattern": "diagonal",
        "repeated_numbers": "repeat",
    }
    for source_key, rule_key in key_map.items():
        rule = rules_by_key.get(rule_key)
        if not rule:
            return False
        if source_key == "repeated_numbers":
            continue
        if not _snapshot_source_numbers(rule.get("candidate_numbers") or rule.get("candidate_groups")):
            return False
    return True


def _dashboard_ready(snapshot: dict) -> bool:
    rules = snapshot.get("rules")
    if not isinstance(rules, list):
        return False
    return any(
        isinstance(item, dict) and item.get("status") in {"ready", "experimental"}
        for item in rules
    )
