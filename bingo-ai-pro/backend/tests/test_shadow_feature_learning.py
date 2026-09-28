from analysis.shadow_feature_learning import (
    build_shadow_snapshot,
    omission_ages,
    pair_lift,
    super_omission_ages,
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
