"""Read-only, deterministic top-5/top-6 evaluation helpers.

Never use actual draw numbers to select candidates. Call rank_candidates at
prediction time with scores known at that time, persist the selections, and
call evaluate_selection only after the target draw is published.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from datetime import datetime, timezone
from hashlib import sha256
import json


def freeze_selection(recommended_twenty, scores, source_issue, target_issue):
    """Bounded, synchronous snapshot; no database, worker, or weight writes."""
    source_issue, target_issue = str(source_issue), str(target_issue)
    if not (source_issue.isdecimal() and target_issue.isdecimal() and int(target_issue) == int(source_issue) + 1):
        raise ValueError('source and target must be consecutive numeric issues')
    ranked = rank_candidates(recommended_twenty, scores)
    payload = {
        **ranked, 'source_issue': str(source_issue), 'target_issue': str(target_issue),
        'ranking_scores': {str(n): float(scores[n]) for n in sorted(recommended_twenty)},
        'production_twenty': sorted(recommended_twenty),
        'frozen_at': datetime.now(timezone.utc).isoformat(),
        'status': 'waiting_draw', 'version': 'top-k-shadow-v2',
    }
    payload['snapshot_sha256'] = snapshot_hash(payload)
    return payload


def snapshot_hash(payload):
    content = {key: value for key, value in payload.items() if key != 'snapshot_sha256'}
    return sha256(json.dumps(content, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def rank_candidates(recommended_twenty: Sequence[int], scores: Mapping[int, float]) -> dict[str, list[int]]:
    """Rank strictly within the frozen 20-number production recommendation."""
    numbers = list(recommended_twenty)
    if len(numbers) != 20 or len(set(numbers)) != 20 or any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 80 for n in numbers):
        raise ValueError("recommended_twenty must contain 20 distinct integers from 1 to 80")
    for n in numbers:
        if n not in scores or not isfinite(float(scores[n])):
            raise ValueError(f"missing or non-finite prediction-time score for {n}")
    ordered = sorted(numbers, key=lambda n: (-float(scores[n]), n))
    return {"top5": ordered[:5], "top6": ordered[:6]}


def evaluate_selection(selection: Sequence[int], actual_twenty: Sequence[int]) -> int:
    """Score a previously frozen selection against the actual target draw."""
    picks, actual = list(selection), list(actual_twenty)
    if len(picks) not in (5, 6) or len(set(picks)) != len(picks):
        raise ValueError("selection must contain 5 or 6 distinct numbers")
    if len(actual) != 20 or len(set(actual)) != 20:
        raise ValueError("actual_twenty must contain 20 distinct numbers")
    if any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 80 for n in picks + actual):
        raise ValueError("all numbers must be integers from 1 to 80")
    return len(set(picks).intersection(actual))


def random_baseline(selection_size: int) -> float:
    if selection_size not in (5, 6):
        raise ValueError("selection_size must be 5 or 6")
    return selection_size / 4.0


def evaluate_frozen_prediction(record: Mapping) -> dict:
    """Evaluate only a previously persisted shadow snapshot after verification.

    Does not infer historical selections from post-draw scores.
    """
    metadata = record.get("fast_path_metadata") or {}
    shadow = metadata.get("top_k_shadow") if isinstance(metadata, dict) else None
    if not isinstance(shadow, dict):
        return {"status": "unavailable", "reason": "no_frozen_shadow"}
    target = str(record.get("prediction_issue") or record.get("target_issue") or "")
    if not target or str(shadow.get("target_issue") or "") != target:
        return {"status": "unavailable", "reason": "target_mismatch"}
    status = str(record.get("prediction_status") or "")
    if status in ("waiting_draw", "pending", "predicted"):
        return {"status": "waiting_draw"}
    if status not in ("verified", "learned"):
        return {"status": "unavailable", "reason": "not_verified"}
    if str(record.get("verified_issue") or "") != target:
        return {"status": "unavailable", "reason": "verification_issue_mismatch"}
    # Require the frozen selections to belong to the persisted production 20.
    recommended = record.get("recommend_numbers") or []
    if (
        not isinstance(recommended, list)
        or len(recommended) != 20
        or any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 80 for n in recommended)
        or len(set(recommended)) != 20
    ):
        return {"status": "unavailable", "reason": "invalid_production_twenty"}
    source = str(record.get("issue") or "")
    if not source or str(shadow.get("source_issue") or "") != source:
        return {"status": "unavailable", "reason": "source_issue_mismatch"}
    if not (source.isdecimal() and target.isdecimal() and int(target) == int(source) + 1):
        return {"status": "unavailable", "reason": "nonconsecutive_prediction_issue"}
    actual = record.get("winning_numbers") or []
    top5, top6 = shadow.get("top5"), shadow.get("top6")
    try:
        if not isinstance(top5, list) or not isinstance(top6, list) or len(top5) != 5 or len(top6) != 6 or top6[:5] != top5 or not set(top6).issubset(set(recommended)):
            raise ValueError("invalid nested selections")
        if shadow.get('version') == 'top-k-shadow-v2':
            if shadow.get('production_twenty') != sorted(recommended) or shadow.get('snapshot_sha256') != snapshot_hash(shadow):
                raise ValueError('snapshot integrity mismatch')
            if not isinstance(shadow.get('ranking_scores'), dict) or set(shadow['ranking_scores']) != {str(n) for n in recommended}:
                raise ValueError('invalid ranking scores')
            scores = {int(n): score for n, score in shadow['ranking_scores'].items()}
            if rank_candidates(recommended, scores) != {'top5': top5, 'top6': top6}:
                raise ValueError('ranking mismatch')
            if datetime.fromisoformat(shadow['frozen_at']).tzinfo is None:
                raise ValueError('freeze time needs timezone')
        return {
            "status": "verified", "target_issue": target,
            "top5": top5, "top6": top6,
            "top5_hits": evaluate_selection(top5, actual),
            "top6_hits": evaluate_selection(top6, actual),
            "random_top5_expected": random_baseline(5),
            "random_top6_expected": random_baseline(6),
        }
    except (ValueError, TypeError, KeyError, OverflowError):
        return {"status": "unavailable", "reason": "invalid_frozen_data"}
