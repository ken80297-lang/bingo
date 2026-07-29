from pathlib import Path

import desktop.core.phase2_1_validation as validation


MASTER = r"C:\Users\ken80297\Desktop\master_draws.csv"


def test_phase2_1_metric_definition_and_equal_size_baseline(monkeypatch):
    monkeypatch.setattr(validation, "BOOTSTRAP_ROUNDS", 200)

    result = validation.run_phase2_1_validation(MASTER)

    assert result["metric_definition_audit"]["success_rate_redefined"] is True
    assert result["overall_significance"]["sample_size"] == 5990
    assert result["high5_significance"]["expected_baseline"] == 1.25
    assert result["super_candidate_significance"]["expected_baseline"] == 1 / 80
    assert result["rule_equal_size_random_baseline"]
    assert all("expected_random_hits" in row for row in result["rule_equal_size_random_baseline"])
    assert all("normalized_lift" in row for row in result["rule_equal_size_random_baseline"])


def test_phase2_1_bootstrap_reproducible(monkeypatch):
    monkeypatch.setattr(validation, "BOOTSTRAP_ROUNDS", 200)

    first = validation._bootstrap_ci([1, 2, 3, 4, 5], 200, validation.BOOTSTRAP_SEED)
    second = validation._bootstrap_ci([1, 2, 3, 4, 5], 200, validation.BOOTSTRAP_SEED)

    assert first == second


def test_phase2_1_walk_forward_and_multiple_testing(monkeypatch):
    monkeypatch.setattr(validation, "BOOTSTRAP_ROUNDS", 200)

    result = validation.run_phase2_1_validation(MASTER)

    folds = result["walk_forward"]["folds"]
    assert folds
    assert result["walk_forward"]["summary"]["fold_count"] == len(folds)
    assert all(fold["validation_count"] <= 200 for fold in folds)
    assert result["phase2_report"]["no_look_ahead"] is True
    assert result["multiple_testing_results"]
    assert all("bonferroni_p" in row for row in result["multiple_testing_results"])


def test_phase2_1_majority_baselines_and_export(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "BOOTSTRAP_ROUNDS", 200)
    output = tmp_path / "phase2_1_validation"

    result = validation.export_phase2_1_validation(MASTER, output)

    assert result["validation"]["big_small_baselines"]
    assert result["validation"]["odd_even_baselines"]
    expected = {
        "metric_definition_audit.json",
        "overall_significance.json",
        "high5_significance.json",
        "super_candidate_significance.json",
        "big_small_baselines.csv",
        "odd_even_baselines.csv",
        "rule_equal_size_random_baseline.csv",
        "rule_enabled_vs_disabled.csv",
        "walk_forward_folds.csv",
        "walk_forward_summary.json",
        "rule_candidate_count_analysis.csv",
        "multiple_testing_results.csv",
        "losing_streak_analysis.csv",
        "phase2_1_report.txt",
    }
    assert expected == {item.name for item in output.iterdir()}


def test_phase2_1_master_file_exists():
    assert Path(MASTER).exists()

