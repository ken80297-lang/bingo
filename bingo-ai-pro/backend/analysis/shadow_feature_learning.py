"""Shadow feature learning for Bingo AI Pro.

Pure feature extraction only: this module never changes production prediction
weights. Draws are expected newest-first and only information present in the
supplied history is used.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations
from math import comb

WINDOWS = (5, 10, 20, 30, 50, 100, 300)
UNIVERSE_SIZE = 80
DRAW_SIZE = 20


def _numbers(draw: dict) -> tuple[int, ...]:
    return tuple(sorted({int(n) for n in draw.get("numbers", []) if 1 <= int(n) <= 80}))


def _super(draw: dict) -> int | None:
    raw = draw.get("super_number", draw.get("super"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 80 else None


def omission_ages(draws: list[dict]) -> dict[int, int]:
    """Consecutive draws since each number last appeared."""
    ages = {}
    for number in range(1, 81):
        age = 0
        for draw in draws:
            if number in _numbers(draw):
                break
            age += 1
        ages[number] = age
    return ages


def super_omission_ages(draws: list[dict]) -> dict[int, int]:
    ages = {}
    supers = [_super(draw) for draw in draws]
    for number in range(1, 81):
        age = 0
        for value in supers:
            if value == number:
                break
            age += 1
        ages[number] = age
    return ages


def pair_lift(draws: list[dict], window: int = 30) -> list[dict]:
    """Rank pair co-occurrence against the random 20-of-80 baseline."""
    sample = [_numbers(d) for d in draws[:window]]
    sample = [nums for nums in sample if len(nums) == DRAW_SIZE]
    n = len(sample)
    if not n:
        return []

    counts = Counter()
    for nums in sample:
        counts.update(combinations(nums, 2))

    baseline = comb(DRAW_SIZE, 2) / comb(UNIVERSE_SIZE, 2)
    rows = []
    for pair, count in counts.items():
        rate = count / n
        rows.append({
            "numbers": list(pair),
            "count": count,
            "rate": rate,
            "baseline": baseline,
            "lift": rate / baseline,
        })
    return sorted(rows, key=lambda row: (row["lift"], row["count"], row["numbers"]), reverse=True)


def triple_lift(draws: list[dict], window: int = 30) -> list[dict]:
    sample = [_numbers(d) for d in draws[:window]]
    sample = [nums for nums in sample if len(nums) == DRAW_SIZE]
    n = len(sample)
    if not n:
        return []

    counts = Counter()
    for nums in sample:
        counts.update(combinations(nums, 3))

    baseline = comb(DRAW_SIZE, 3) / comb(UNIVERSE_SIZE, 3)
    rows = []
    for triple, count in counts.items():
        rate = count / n
        rows.append({
            "numbers": list(triple),
            "count": count,
            "rate": rate,
            "baseline": baseline,
            "lift": rate / baseline,
        })
    return sorted(rows, key=lambda row: (row["lift"], row["count"], row["numbers"]), reverse=True)


def super_features(draws: list[dict], window: int = 30) -> dict:
    sample = draws[:window]
    supers = [value for value in (_super(d) for d in sample) if value is not None]
    number_counts = Counter(supers)
    tail_counts = Counter(value % 10 for value in supers)
    return {
        "sample_size": len(supers),
        "number_counts": dict(sorted(number_counts.items())),
        "tail_counts": dict(sorted(tail_counts.items())),
        "number_omission": super_omission_ages(draws),
    }


def build_shadow_snapshot(draws: list[dict]) -> dict:
    """Build diagnostics for learning/verification without production influence."""
    available = [w for w in WINDOWS if len(draws) >= w]
    windows = available or ([len(draws)] if draws else [])
    return {
        "mode": "shadow",
        "production_weight_effect": False,
        "history_size": len(draws),
        "windows": {
            str(window): {
                "top_pairs": pair_lift(draws, window)[:20],
                "top_triples": triple_lift(draws, window)[:12],
                "super": super_features(draws, window),
            }
            for window in windows
        },
        "number_omission": omission_ages(draws),
    }


def score_shadow_snapshot(snapshot: dict, official_numbers: list[int], official_super: int | None = None) -> dict:
    """Score frozen pre-draw shadow features against the next official draw."""
    official = set(int(n) for n in official_numbers or [] if 1 <= int(n) <= 80)
    if len(official) != DRAW_SIZE:
        return {"status": "pending_official", "production_weight_effect": False}

    omission = snapshot.get("number_omission") or {}
    omission_hits = Counter()
    omission_totals = Counter()
    for raw_number, raw_age in omission.items():
        try:
            number, age = int(raw_number), int(raw_age)
        except (TypeError, ValueError):
            continue
        bucket = "0" if age == 0 else "1-2" if age <= 2 else "3-5" if age <= 5 else "6-10" if age <= 10 else "11+"
        omission_totals[bucket] += 1
        if number in official:
            omission_hits[bucket] += 1

    window_scores = {}
    for window, payload in (snapshot.get("windows") or {}).items():
        pairs = payload.get("top_pairs") or []
        triples = payload.get("top_triples") or []
        pair_hits = [row for row in pairs if set(map(int, row.get("numbers") or [])) <= official]
        triple_hits = [row for row in triples if set(map(int, row.get("numbers") or [])) <= official]

        super_payload = payload.get("super") or {}
        super_omission = super_payload.get("number_omission") or {}
        super_age = None
        if official_super is not None:
            super_age = super_omission.get(official_super, super_omission.get(str(official_super)))

        tail_counts = super_payload.get("tail_counts") or {}
        ranked_tails = sorted(
            ((int(tail), int(count)) for tail, count in tail_counts.items()),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
        top_tails = [tail for tail, _ in ranked_tails[:3]]
        super_tail_hit = bool(official_super is not None and official_super % 10 in top_tails)

        window_scores[str(window)] = {
            "pair_candidates": len(pairs),
            "pair_hits": len(pair_hits),
            "pair_hit_examples": [row.get("numbers") for row in pair_hits[:5]],
            "triple_candidates": len(triples),
            "triple_hits": len(triple_hits),
            "triple_hit_examples": [row.get("numbers") for row in triple_hits[:5]],
            "super_top_tails": top_tails,
            "super_tail_hit": super_tail_hit,
            "official_super_omission_age": super_age,
        }

    return {
        "status": "scored",
        "mode": "shadow",
        "production_weight_effect": False,
        "official_count": len(official),
        "official_super": official_super,
        "random_baselines": {"number_hit_rate": DRAW_SIZE / UNIVERSE_SIZE, "super_tail_top3": 0.3},
        "omission_buckets": {
            bucket: {
                "candidates": omission_totals[bucket],
                "hits": omission_hits[bucket],
                "hit_rate": round(omission_hits[bucket] / omission_totals[bucket], 6)
                if omission_totals[bucket] else 0,
            }
            for bucket in sorted(omission_totals)
        },
        "windows": window_scores,
    }


def aggregate_shadow_performance(verifications: list[dict], horizons: tuple[int, ...] = (20, 50, 100)) -> dict:
    """Aggregate already-scored shadow results; descriptive only, never weights."""
    scored = [v for v in verifications if (v or {}).get("status") == "scored"]
    output = {"mode": "shadow", "production_weight_effect": False, "available_samples": len(scored), "horizons": {}}
    for horizon in horizons:
        sample = scored[:horizon]
        windows = {}
        omission = {}
        for result in sample:
            for bucket, row in (result.get("omission_buckets") or {}).items():
                agg = omission.setdefault(bucket, {"candidates": 0, "hits": 0})
                agg["candidates"] += int(row.get("candidates") or 0)
                agg["hits"] += int(row.get("hits") or 0)
            for window, row in (result.get("windows") or {}).items():
                agg = windows.setdefault(str(window), {
                    "pair_candidates": 0, "pair_hits": 0,
                    "triple_candidates": 0, "triple_hits": 0,
                    "super_tail_trials": 0, "super_tail_hits": 0,
                })
                agg["pair_candidates"] += int(row.get("pair_candidates") or 0)
                agg["pair_hits"] += int(row.get("pair_hits") or 0)
                agg["triple_candidates"] += int(row.get("triple_candidates") or 0)
                agg["triple_hits"] += int(row.get("triple_hits") or 0)
                if row.get("super_top_tails"):
                    agg["super_tail_trials"] += 1
                    agg["super_tail_hits"] += int(bool(row.get("super_tail_hit")))

        for row in omission.values():
            row["hit_rate"] = round(row["hits"] / row["candidates"], 6) if row["candidates"] else 0
            row["random_baseline"] = DRAW_SIZE / UNIVERSE_SIZE
            row["baseline_delta"] = round(row["hit_rate"] - row["random_baseline"], 6)
            row["lift_vs_random"] = round(row["hit_rate"] / row["random_baseline"], 6) if row["random_baseline"] else 0
        pair_baseline = comb(DRAW_SIZE, 2) / comb(UNIVERSE_SIZE, 2)
        triple_baseline = comb(DRAW_SIZE, 3) / comb(UNIVERSE_SIZE, 3)
        for row in windows.values():
            row["pair_hit_rate"] = round(row["pair_hits"] / row["pair_candidates"], 6) if row["pair_candidates"] else 0
            row["triple_hit_rate"] = round(row["triple_hits"] / row["triple_candidates"], 6) if row["triple_candidates"] else 0
            row["super_tail_hit_rate"] = round(row["super_tail_hits"] / row["super_tail_trials"], 6) if row["super_tail_trials"] else 0
            row["pair_random_baseline"] = round(pair_baseline, 6)
            row["pair_baseline_delta"] = round(row["pair_hit_rate"] - pair_baseline, 6)
            row["pair_lift_vs_random"] = round(row["pair_hit_rate"] / pair_baseline, 6) if pair_baseline else 0
            row["triple_random_baseline"] = round(triple_baseline, 6)
            row["triple_baseline_delta"] = round(row["triple_hit_rate"] - triple_baseline, 6)
            row["triple_lift_vs_random"] = round(row["triple_hit_rate"] / triple_baseline, 6) if triple_baseline else 0
            row["super_tail_random_baseline"] = 0.3
            row["super_tail_baseline_delta"] = round(row["super_tail_hit_rate"] - 0.3, 6)
            row["super_tail_lift_vs_random"] = round(row["super_tail_hit_rate"] / 0.3, 6)

        output["horizons"][str(horizon)] = {
            "sample_size": len(sample),
            "complete": len(sample) >= horizon,
            "omission_buckets": omission,
            "windows": windows,
        }
    return output


def assess_shadow_stability(performance: dict, minimum_samples: int = 20) -> dict:
    """Flag candidate signals only when rolling horizons are directionally stable."""
    horizons = performance.get("horizons") or {}
    result = {
        "mode": "shadow",
        "production_weight_effect": False,
        "minimum_samples": minimum_samples,
        "signals": {},
    }

    def assess(name: str, values: list[tuple[int, float]]) -> None:
        eligible = [(n, delta) for n, delta in values if n >= minimum_samples]
        positive = [delta for _, delta in eligible if delta > 0]
        stable = len(eligible) >= 2 and len(positive) == len(eligible)
        result["signals"][name] = {
            "status": "candidate_positive" if stable else "insufficient_or_unstable",
            "eligible_horizons": len(eligible),
            "positive_horizons": len(positive),
            "deltas": [round(delta, 6) for _, delta in eligible],
            "production_eligible": False,
        }

    omission_names = set()
    window_names = set()
    for payload in horizons.values():
        omission_names.update((payload.get("omission_buckets") or {}).keys())
        window_names.update((payload.get("windows") or {}).keys())

    for bucket in sorted(omission_names):
        values = []
        for payload in horizons.values():
            sample_size = int(payload.get("sample_size") or 0)
            row = (payload.get("omission_buckets") or {}).get(bucket) or {}
            if "baseline_delta" in row:
                values.append((sample_size, float(row["baseline_delta"])))
        assess(f"omission:{bucket}", values)

    for window in sorted(window_names, key=lambda value: int(value)):
        for metric in ("pair", "triple", "super_tail"):
            values = []
            for payload in horizons.values():
                sample_size = int(payload.get("sample_size") or 0)
                row = (payload.get("windows") or {}).get(window) or {}
                key = f"{metric}_baseline_delta"
                if key in row:
                    values.append((sample_size, float(row[key])))
            assess(f"{metric}:window_{window}", values)

    return result


def rank_shadow_signals(performance: dict, stability: dict, minimum_samples: int = 20) -> dict:
    """Rank observation candidates and retire persistently non-positive signals."""
    horizons = performance.get("horizons") or {}
    signals = stability.get("signals") or {}
    ranked = []

    for name, gate in signals.items():
        deltas = [float(value) for value in gate.get("deltas") or []]
        avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
        positive_ratio = (
            int(gate.get("positive_horizons") or 0) / int(gate.get("eligible_horizons") or 1)
            if int(gate.get("eligible_horizons") or 0) else 0.0
        )
        score = round((avg_delta * 100) + (positive_ratio * 10), 4)

        if gate.get("status") == "candidate_positive":
            lifecycle = "observe_candidate"
        elif int(gate.get("eligible_horizons") or 0) >= 2 and deltas and max(deltas) <= 0:
            lifecycle = "retire_candidate"
        else:
            lifecycle = "collect_more"

        ranked.append({
            "signal": name,
            "score": score,
            "average_baseline_delta": round(avg_delta, 6),
            "eligible_horizons": int(gate.get("eligible_horizons") or 0),
            "positive_horizons": int(gate.get("positive_horizons") or 0),
            "lifecycle": lifecycle,
            "production_eligible": False,
        })

    ranked.sort(key=lambda row: (row["score"], row["signal"]), reverse=True)
    for index, row in enumerate(ranked, 1):
        row["rank"] = index

    return {
        "mode": "shadow",
        "production_weight_effect": False,
        "minimum_samples": minimum_samples,
        "candidate_count": sum(row["lifecycle"] == "observe_candidate" for row in ranked),
        "retire_count": sum(row["lifecycle"] == "retire_candidate" for row in ranked),
        "signals": ranked,
    }
