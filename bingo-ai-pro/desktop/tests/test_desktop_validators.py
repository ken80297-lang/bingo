from desktop.core.validators import is_production_issue, normalize_numbers, validate_draw, validate_prediction


def test_normalize_numbers_keeps_unique_range_sorted():
    assert normalize_numbers(["5", 1, 1, 81, "x", 3]) == [1, 3, 5]


def test_production_issue_filters_test_scope():
    assert is_production_issue("114000001")
    assert not is_production_issue("TEST-1")
    assert not is_production_issue("99000001")


def test_validate_draw_requires_20_numbers():
    draw = {"issue": "114000001", "numbers": list(range(1, 21)), "super_number": 7, "source": "taiwan_lottery"}
    assert validate_draw(draw) == (True, None)
    invalid = {**draw, "numbers": [1, 2, 3]}
    assert validate_draw(invalid)[0] is False


def test_validate_prediction_requires_source_target_pair():
    prediction = {
        "issue": "114000001",
        "prediction_issue": "114000002",
        "recommend_numbers": list(range(1, 21)),
        "strategy": "production",
    }
    assert validate_prediction(prediction) == (True, None)
    assert validate_prediction({**prediction, "prediction_issue": "114000003"})[1] == "target_must_follow_source"

