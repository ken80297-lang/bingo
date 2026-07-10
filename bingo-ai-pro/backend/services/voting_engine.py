from __future__ import annotations

from collections import Counter, defaultdict

from services.model_engine import MODEL_NAMES, model_hit_rates, run_all_models


def _stars(confidence: float) -> str:
    count = max(1, min(5, round(float(confidence or 0) / 20)))
    return "★" * count + "☆" * (5 - count)


def build_voting_result(limit: int = 100) -> dict:
    model_payload = run_all_models(limit)
    models = model_payload.get("models") or []
    votes: Counter[int] = Counter()
    reasons = []
    model_scores = {}

    for model in models:
        confidence = float(model.get("confidence") or 0)
        model_key = model.get("model")
        model_scores[model_key] = {
            "label": model.get("label"),
            "confidence": round(confidence, 2),
            "stars": _stars(confidence),
            "reason": model.get("reason"),
            "candidate_numbers": model.get("candidate_numbers") or [],
        }
        weight = max(1, confidence / 20)
        for rank, number in enumerate(model.get("candidate_numbers") or []):
            votes[int(number)] += weight + max(0, 10 - rank) * 0.15
        if model.get("reason"):
            reasons.append(model["reason"])

    final_candidates = [number for number, _ in votes.most_common(10)]
    confidence = 0
    if models:
        confidence = sum(float(model.get("confidence") or 0) for model in models) / len(models)
        if final_candidates:
            confidence += min(12, votes[final_candidates[0]] / 2)
    winning_model = None
    if model_scores:
        winning_model = max(model_scores.items(), key=lambda item: item[1]["confidence"])[0]

    hit_rates = model_hit_rates(100)
    ranking = sorted(
        [
            {
                "model": key,
                "label": MODEL_NAMES.get(key, key),
                "confidence": model_scores.get(key, {}).get("confidence", 0),
                "recent_hit_rate": hit_rates.get(key, 0),
                "stars": model_scores.get(key, {}).get("stars", "☆☆☆☆☆"),
            }
            for key in MODEL_NAMES
        ],
        key=lambda item: (item["recent_hit_rate"], item["confidence"]),
        reverse=True,
    )

    return {
        "status": model_payload.get("status", "ok"),
        "latest_issue": model_payload.get("latest_issue"),
        "models": models,
        "model_scores": model_scores,
        "model_ranking": ranking,
        "winning_model": winning_model,
        "final_candidates": final_candidates,
        "confidence": round(max(1, min(100, confidence)), 2),
        "reason": reasons[:5],
    }


def model_status() -> dict:
    voting = build_voting_result(100)
    return {
        "status": voting.get("status"),
        "latest_issue": voting.get("latest_issue"),
        "models": voting.get("model_scores"),
        "ranking": voting.get("model_ranking"),
        "final_candidates": voting.get("final_candidates"),
        "confidence": voting.get("confidence"),
    }
