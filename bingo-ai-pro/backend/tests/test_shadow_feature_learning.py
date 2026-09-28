from analysis.shadow_feature_learning import (
    build_shadow_snapshot,
    omission_ages,
    pair_lift,
    super_omission_ages,
    score_shadow_snapshot,
    aggregate_shadow_performance,
)


def _draw(numbers, super_number=None):
    return {"numbers": numbers, "super_number": super_number}


def test_omission_is_newest_first_and_no_future_data():
    draws = [
        _draw(list(range(1, 21)), 5),
        _draw(list(range(21, 41)), 25),
        _draw(list(range(41, 61)), 45),
    ]
    ages = omission_ages(draws)
    assert ages[1] == 0
    assert ages[21] == 1
    assert ages[41] == 2
    assert ages[80] == 3


def test_super_omission_tracks_actual_super_number():
    draws = [
        _draw(list(range(1, 21)), 5),
        _draw(list(range(21, 41)), 25),
        _draw(list(range(41, 61)), 5),
    ]
    ages = super_omission_ages(draws)
    assert ages[5] == 0
    assert ages[25] == 1
    assert ages[80] == 3


def test_pair_lift_uses_valid_twenty_number_draws_only():
    nums = list(range(1, 21))
    rows = pair_lift([_draw(nums), _draw(nums)], window=30)
    pair = next(row for row in rows if row["numbers"] == [1, 2])
    assert pair["count"] == 2
    assert pair["lift"] > 1


def test_snapshot_is_explicitly_shadow_only():
    snapshot = build_shadow_snapshot([_draw(list(range(1, 21)), 5)])
    assert snapshot["mode"] == "shadow"
    assert snapshot["production_weight_effect"] is False


def test_shadow_scoring_measures_pairs_omission_and_super_without_weight_effect():
    snapshot = {
        "mode": "shadow",
        "production_weight_effect": False,
        "number_omission": {1: 0, 2: 2, 30: 7, 40: 12},
        "windows": {
            "30": {
                "top_pairs": [{"numbers": [1, 2]}, {"numbers": [30, 40]}],
                "top_triples": [{"numbers": [1, 2, 3]}],
                "super": {
                    "tail_counts": {5: 4, 2: 3, 9: 1},
                    "number_omission": {25: 8},
                },
            }
        },
    }

    result = score_shadow_snapshot(snapshot, list(range(1, 21)), 25)

    assert result["status"] == "scored"
    assert result["production_weight_effect"] is False
    assert result["windows"]["30"]["pair_hits"] == 1
    assert result["windows"]["30"]["triple_hits"] == 1
    assert result["windows"]["30"]["super_tail_hit"] is True
    assert result["windows"]["30"]["official_super_omission_age"] == 8
    assert result["omission_buckets"]["0"]["hits"] == 1
    assert result["omission_buckets"]["1-2"]["hits"] == 1


def test_shadow_scoring_waits_for_complete_official_draw():
    result = score_shadow_snapshot({"windows": {}}, [1, 2, 3], 1)
    assert result["status"] == "pending_official"
    assert result["production_weight_effect"] is False


def test_aggregate_shadow_performance_rolls_up_multiple_issues():
    rows = [
        {
            "status": "scored",
            "omission_buckets": {"3-5": {"candidates": 10, "hits": 3}},
            "windows": {"30": {
                "pair_candidates": 20, "pair_hits": 2,
                "triple_candidates": 12, "triple_hits": 1,
                "super_top_tails": [1, 2, 3], "super_tail_hit": True,
            }},
        },
        {
            "status": "scored",
            "omission_buckets": {"3-5": {"candidates": 10, "hits": 2}},
            "windows": {"30": {
                "pair_candidates": 20, "pair_hits": 1,
                "triple_candidates": 12, "triple_hits": 0,
                "super_top_tails": [4, 5, 6], "super_tail_hit": False,
            }},
        },
    ]
    result = aggregate_shadow_performance(rows, horizons=(20,))
    h = result["horizons"]["20"]
    assert h["sample_size"] == 2
    assert h["complete"] is False
    assert h["omission_buckets"]["3-5"]["hit_rate"] == 0.25
    assert h["windows"]["30"]["pair_hits"] == 3
    assert h["windows"]["30"]["super_tail_hit_rate"] == 0.5
    assert result["production_weight_effect"] is False
