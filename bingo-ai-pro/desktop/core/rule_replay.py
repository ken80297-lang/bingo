from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import mean
from typing import Callable

from desktop.core.replay_dataset import ReplayDraw
from desktop.core.rule_order import FIXED_RULE_ORDER, RULE_NAME_ZH


@dataclass(frozen=True)
class RuleReplayResult:
    rule_key: str
    rule_name_zh: str
    score: float
    confidence: float
    candidates: list[int]
    summary: str
    evidence: dict
    status: str


RuleFunction = Callable[[list[ReplayDraw], ReplayDraw], RuleReplayResult]


def replay_all_rules(history: list[ReplayDraw], source_draw: ReplayDraw) -> list[RuleReplayResult]:
    functions: dict[str, RuleFunction] = {
        "hot": _hot_rule,
        "cold": _cold_rule,
        "missing": _missing_rule,
        "repeat": _repeat_rule,
        "tail": _tail_rule,
        "gap": _gap_rule,
        "cluster": _cluster_rule,
        "diagonal": _diagonal_rule,
        "super": _super_rule,
        "laowanjia": _laowanjia_rule,
        "ladder": _ladder_rule,
        "partial_ladder": _partial_ladder_rule,
        "extended_ladder": _extended_ladder_rule,
        "reverse": _reverse_rule,
        "neighbor": _neighbor_rule,
        "guide": _guide_rule,
        "integrated": _integrated_rule,
        "sunset": _sunset_rule,
        "momentum": _momentum_rule,
        "super_number_trajectory_recovery": _super_recovery_rule,
        "cluster_aftershock_recovery": _cluster_aftershock_rule,
    }
    results = []
    for key, _ in FIXED_RULE_ORDER:
        result = functions[key](history, source_draw)
        results.append(result)
    return results


def _make_result(rule_key: str, candidates: list[int], score: float, confidence: float, summary: str, evidence: dict | None = None) -> RuleReplayResult:
    unique = _unique_numbers(candidates)[:20]
    return RuleReplayResult(
        rule_key=rule_key,
        rule_name_zh=RULE_NAME_ZH[rule_key],
        score=round(score, 4),
        confidence=round(confidence, 4),
        candidates=unique,
        summary=summary,
        evidence=evidence or {},
        status="ok" if unique else "empty",
    )


def _hot_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    counts = _number_counts(history[-60:])
    candidates = [number for number, _ in counts.most_common(20)]
    return _make_result("hot", candidates, _coverage_score(candidates, source.numbers), min(1, len(history) / 60), "recent frequency leaders", {"window": min(len(history), 60)})


def _cold_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    counts = _number_counts(history[-60:])
    candidates = sorted(range(1, 81), key=lambda number: (counts.get(number, 0), number))[:20]
    return _make_result("cold", candidates, 1 - _coverage_score(candidates, source.numbers), min(1, len(history) / 60), "least frequent recent numbers")


def _missing_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    last_seen = {number: -1 for number in range(1, 81)}
    for index, draw in enumerate(history):
        for number in draw.numbers:
            last_seen[number] = index
    candidates = sorted(range(1, 81), key=lambda number: (last_seen[number], number))[:20]
    return _make_result("missing", candidates, 0.65, min(1, len(history) / 80), "longest missing numbers")


def _repeat_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    previous = history[-1].numbers if history else source.numbers
    return _make_result("repeat", previous, 0.5, 0.55, "previous issue repeat candidates")


def _tail_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    tails = Counter(number % 10 for draw in history[-30:] for number in draw.numbers)
    selected_tails = [tail for tail, _ in tails.most_common(3)]
    candidates = [number for number in range(1, 81) if number % 10 in selected_tails]
    return _make_result("tail", candidates, 0.58, 0.6, "dominant recent tails", {"tails": selected_tails})


def _gap_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    gaps = [b - a for a, b in zip(source.numbers, source.numbers[1:])]
    avg_gap = mean(gaps) if gaps else 4
    anchors = source.numbers[::4]
    candidates = [number + round(avg_gap) for number in anchors] + [number - round(avg_gap) for number in anchors]
    return _make_result("gap", candidates, min(1, avg_gap / 10), 0.52, "source gap projection", {"average_gap": round(avg_gap, 2)})


def _cluster_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    zones = Counter((number - 1) // 10 for number in source.numbers)
    top_zones = [zone for zone, _ in zones.most_common(2)]
    candidates = [number for number in range(1, 81) if (number - 1) // 10 in top_zones]
    return _make_result("cluster", candidates, max(zones.values()) / 20 if zones else 0, 0.62, "source cluster continuation", {"zones": top_zones})


def _diagonal_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    candidates = [number + 9 for number in source.numbers] + [number - 9 for number in source.numbers]
    return _make_result("diagonal", candidates, 0.5, 0.48, "8x10 board diagonal neighbors")


def _super_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    supers = [draw.super_number for draw in history[-40:] if draw.super_number]
    counts = Counter(supers)
    candidates = [number for number, _ in counts.most_common(20)]
    return _make_result("super", candidates, 0.7 if candidates else 0, min(1, len(supers) / 40), "frequent super numbers")


def _laowanjia_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    candidates = _unique_numbers(source.numbers[:5] + _neighbor_numbers(source.numbers[:8]) + _tail_mates(source.numbers[:5]))
    return _make_result("laowanjia", candidates, 0.57, 0.58, "legacy neighbor and tail mix")


def _ladder_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    candidates = [number for start in range(1, 10) for number in (start, start + 10, start + 20)]
    return _make_result("ladder", candidates, 0.45, 0.42, "vertical ladder candidates")


def _partial_ladder_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    candidates = [number for number in range(1, 81) if number % 10 in {1, 4, 7}]
    return _make_result("partial_ladder", candidates, 0.43, 0.4, "partial ladder tails")


def _extended_ladder_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    candidates = [number for number in source.numbers for number in (number + 10, number + 20, number - 10)]
    return _make_result("extended_ladder", candidates, 0.49, 0.44, "extended ladder from source")


def _reverse_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    candidates = [81 - number for number in source.numbers]
    return _make_result("reverse", candidates, 0.46, 0.39, "reverse board mirror")


def _neighbor_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    return _make_result("neighbor", _neighbor_numbers(source.numbers), 0.54, 0.55, "source adjacent numbers")


def _guide_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    anchors = source.numbers[:5]
    candidates = [anchor + offset for anchor in anchors for offset in (3, 6, 9, 12)]
    return _make_result("guide", candidates, 0.5, 0.46, "guide-card offsets")


def _integrated_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    hot = _hot_rule(history, source).candidates[:8]
    missing = _missing_rule(history, source).candidates[:6]
    neighbor = _neighbor_rule(history, source).candidates[:10]
    return _make_result("integrated", hot + missing + neighbor, 0.62, 0.66, "integrated hot missing neighbor mix")


def _sunset_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    candidates = [number for number in range(41, 81) if number not in source.numbers] + source.numbers[-5:]
    return _make_result("sunset", candidates, 0.44, 0.38, "upper-half sunset pressure")


def _momentum_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    recent = history[-5:]
    counts = _number_counts(recent)
    candidates = [number for number, count in counts.most_common() if count >= 2]
    candidates += _hot_rule(history, source).candidates
    return _make_result("momentum", candidates, min(1, len(candidates) / 20), 0.61, "short-term momentum")


def _super_recovery_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    if not history:
        return _make_result("super_number_trajectory_recovery", [], 0, 0, "no super history")
    supers = [draw.super_number for draw in history[-10:] if draw.super_number]
    projected = []
    for previous, current in zip(supers, supers[1:]):
        projected.append(current + (current - previous))
    return _make_result("super_number_trajectory_recovery", projected + supers[::-1], 0.53, 0.5, "super trajectory projection")


def _cluster_aftershock_rule(history: list[ReplayDraw], source: ReplayDraw) -> RuleReplayResult:
    dense_zone = Counter((number - 1) // 10 for number in source.numbers).most_common(1)
    zone = dense_zone[0][0] if dense_zone else 0
    candidates = [number for number in range(1, 81) if abs(((number - 1) // 10) - zone) <= 1]
    return _make_result("cluster_aftershock_recovery", candidates, 0.56, 0.57, "cluster aftershock neighboring zones", {"source_zone": zone})


def _number_counts(draws: list[ReplayDraw]) -> Counter:
    return Counter(number for draw in draws for number in draw.numbers)


def _unique_numbers(numbers: list[int]) -> list[int]:
    output = []
    for number in numbers:
        if 1 <= int(number) <= 80 and int(number) not in output:
            output.append(int(number))
    return output


def _neighbor_numbers(numbers: list[int]) -> list[int]:
    return _unique_numbers([number + offset for number in numbers for offset in (-1, 1)])


def _tail_mates(numbers: list[int]) -> list[int]:
    tails = {number % 10 for number in numbers}
    return [number for number in range(1, 81) if number % 10 in tails]


def _coverage_score(candidates: list[int], source_numbers: list[int]) -> float:
    return len(set(candidates) & set(source_numbers)) / max(1, len(source_numbers))

