from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.analysis_store import _row_to_v7_model_history_record
from services.model_engine import run_all_models


def _full_draw(issue: str, offset: int) -> dict:
    numbers = [((offset * 7 + i * 3) % 80) + 1 for i in range(20)]
    numbers = list(dict.fromkeys(numbers))
    while len(numbers) < 20:
        candidate = ((offset + len(numbers) * 11) % 80) + 1
        if candidate not in numbers:
            numbers.append(candidate)
    return {
        "issue": issue,
        "numbers": numbers,
        "hot_numbers": numbers[:6],
        "missing_numbers": [n for n in range(1, 81) if n not in numbers][:8],
        "laowanjia_score": 60 + (offset % 20),
        "cluster_score": 55 + (offset % 10),
        "twins": [[numbers[0], numbers[1]]],
        "consecutive": [[numbers[2], numbers[3]]],
        "patch_numbers": numbers[4:10],
        "pattern": "大型群聚 補號模式" if offset % 2 else "冷熱交替 雙生模式",
        "ai_pattern": "連號模式" if offset % 3 == 0 else "",
        # Deliberately include fields excluded by the minimal query.
        "draw_time": "2026-10-06T00:00:00+00:00",
        "super_number": numbers[0],
        "big_small": "大",
        "odd_even": "單",
        "cold_numbers": numbers[-5:],
        "difference_values": {"1": 2},
        "diagonal_pattern": [[numbers[0], numbers[-1]]],
        "ai_score": {"unused": {"candidate_numbers": numbers[:3]}},
        "three_star": numbers[:3],
        "four_star": numbers[:4],
        "five_star": numbers[:5],
        "six_star": numbers[:6],
        "tail_distribution": {"1": 2},
        "hot_zone": [1],
        "cold_zone": [8],
    }


def _minimal_from_full(draw: dict) -> dict:
    row = (
        draw["issue"],
        draw["numbers"],
        draw["hot_numbers"],
        draw["missing_numbers"],
        draw["laowanjia_score"],
        draw["cluster_score"],
        draw["twins"],
        draw["consecutive"],
        draw["patch_numbers"],
        draw["pattern"],
        draw["ai_pattern"],
    )
    return _row_to_v7_model_history_record(row)


def test_v7_minimal_history_preserves_all_model_outputs():
    full = [_full_draw(str(115056700 - i), i) for i in range(100)]
    minimal = [_minimal_from_full(draw) for draw in full]

    full_result = run_all_models(100, draws=full)
    minimal_result = run_all_models(100, draws=minimal)

    assert minimal_result == full_result
    assert len(minimal) == 100
    assert set(minimal[0]) == {
        "issue", "numbers", "hot_numbers", "missing_numbers", "laowanjia_score",
        "cluster_score", "twins", "consecutive", "patch_numbers", "pattern", "ai_pattern",
    }
