from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import recommendation_center, voting_engine


def _simulation():
    return {
        "source_issue": "115000100",
        "results": [
            {"numbers": list(range(1, 11)), "total_score": 90, "scores": {}},
            {"numbers": list(range(11, 21)), "total_score": 80, "scores": {}},
            {"numbers": list(range(21, 31)), "total_score": 70, "scores": {}},
        ],
        "features": {},
    }


def _voting():
    return {
        "status": "ok",
        "models": [{"model": "hotcold"}, {"model": "missing"}],
        "model_scores": {
            "hotcold": {"candidate_numbers": list(range(1, 21)), "confidence": 80},
            "missing": {"candidate_numbers": list(range(21, 41)), "confidence": 75},
        },
        "winning_model": "hotcold",
        "final_candidates": list(range(1, 21)),
        "confidence": 88,
        "trace": [
            {
                "model_name": "Voting Engine",
                "stage": "Final Candidates",
                "input_count": 40,
                "output_count": 20,
                "removed_count": 20,
                "reason": "top_20_by_weight_sorted",
            }
        ],
    }


def test_recommendation_trace_and_20_number_output(monkeypatch):
    monkeypatch.setattr(recommendation_center, "get_simulation_run_by_issue", lambda issue: _simulation())
    monkeypatch.setattr(recommendation_center, "get_latest_simulation_run", lambda: _simulation())
    monkeypatch.setattr(recommendation_center, "get_latest_strategy_rankings", lambda: [])
    monkeypatch.setattr(recommendation_center, "get_active_adaptive_weights", lambda: None)
    monkeypatch.setattr(recommendation_center, "get_data_quality_status", lambda: {"status": "ok"})
    monkeypatch.setattr(recommendation_center, "build_voting_result", lambda limit=100: _voting())
    monkeypatch.setattr(
        recommendation_center,
        "_build_super_recommendation",
        lambda *args, **kwargs: {"based_on_issue": "115000100", "source_issue": "115000100", "recommended": []},
    )

    payload = recommendation_center.calculate_recommendation(
        "115000100",
        "115000101",
        context={"ensure_simulation": False},
    )

    recommendation = payload["recommendation"]
    first_numbers = recommendation["results"][0]["numbers"]

    assert payload["status"] == "ok"
    assert first_numbers == list(range(1, 21))
    assert recommendation["recommendation_output"]["is_valid"] is True
    assert recommendation["recommendation_output"]["output_count"] == 20
    assert any(step["stage"] == "Final Recommendation" for step in recommendation["recommendation_trace"])
    assert any(step["model_name"] == "Model:hotcold" for step in recommendation["recommendation_trace"])


def test_legacy_10_number_simulation_is_expanded_from_model_pool(monkeypatch):
    voting = _voting()
    voting["final_candidates"] = []
    monkeypatch.setattr(recommendation_center, "get_simulation_run_by_issue", lambda issue: _simulation())
    monkeypatch.setattr(recommendation_center, "get_latest_strategy_rankings", lambda: [])
    monkeypatch.setattr(recommendation_center, "get_active_adaptive_weights", lambda: None)
    monkeypatch.setattr(recommendation_center, "get_data_quality_status", lambda: {"status": "ok"})
    monkeypatch.setattr(recommendation_center, "build_voting_result", lambda limit=100: voting)
    monkeypatch.setattr(
        recommendation_center,
        "_build_super_recommendation",
        lambda *args, **kwargs: {"based_on_issue": "115000100", "source_issue": "115000100", "recommended": []},
    )

    payload = recommendation_center.calculate_recommendation(
        "115000100",
        "115000101",
        context={"ensure_simulation": False},
    )

    first_numbers = payload["recommendation"]["results"][0]["numbers"]

    assert len(first_numbers) == 20
    assert len(set(first_numbers)) == 20
    assert first_numbers == sorted(first_numbers)


def test_voting_merge_outputs_20_unique_numbers(monkeypatch):
    monkeypatch.setattr(
        voting_engine,
        "run_all_models",
        lambda limit=100: {
            "status": "ok",
            "latest_issue": "115000100",
            "models": [
                {"model": "hotcold", "label": "HotCold", "confidence": 80, "candidate_numbers": list(range(1, 21))},
                {"model": "missing", "label": "Missing", "confidence": 75, "candidate_numbers": list(range(11, 31))},
            ],
        },
    )
    monkeypatch.setattr(voting_engine, "model_hit_rates", lambda limit=100: {})

    result = voting_engine.build_voting_result()

    assert len(result["final_candidates"]) == 20
    assert len(set(result["final_candidates"])) == 20
    assert result["final_candidates"] == sorted(result["final_candidates"])
    assert any(step["stage"] == "Voting Merge" for step in result["trace"])


def test_production_fast_path_uses_latest_analysis_without_v7_voting(monkeypatch):
    monkeypatch.setattr(
        recommendation_center,
        "get_latest_analysis_history",
        lambda: {
            "issue": "115040918",
            "numbers": list(range(1, 21)),
            "patch_numbers": [21, 22, 23, 24],
            "hot_numbers": [25, 26, 27, 28],
            "missing_numbers": [29, 30, 31, 32],
            "cold_numbers": [33, 34, 35, 36],
            "repeated_numbers": [37, 38],
            "diagonal_pattern": [[39, 40]],
            "super_number": 7,
        },
    )
    monkeypatch.setattr(
        recommendation_center,
        "build_voting_result",
        lambda limit=100: (_ for _ in ()).throw(AssertionError("fast path must not call V7 voting")),
    )

    payload = recommendation_center.calculate_fast_recommendation(
        "115040918",
        "115040919",
        context={"prediction_service": True},
    )

    recommendation = payload["recommendation"]
    numbers = recommendation["results"][0]["numbers"]

    assert payload["status"] == "ok"
    assert len(numbers) == 20
    assert len(set(numbers)) == 20
    assert recommendation["best_strategy"] == "ProductionFastPath"
    assert recommendation["model_voting"]["reason"] == "production_fast_path_does_not_run_v7_voting"
    assert recommendation["timings_ms"]["total_ms"] >= 0


def test_preview_dry_run_does_not_persist_or_record_event(monkeypatch):
    monkeypatch.setattr(
        recommendation_center,
        "calculate_recommendation",
        lambda *args, **kwargs: {
            "status": "ok",
            "recommendation": {
                "issue": "115000100",
                "results": [{"numbers": list(range(1, 21))}],
                "recommendation_output": {"is_valid": True, "output_count": 20},
            },
            "persisted": False,
        },
    )
    monkeypatch.setattr(recommendation_center, "_latest_issue", lambda: "115000100")
    monkeypatch.setattr(recommendation_center, "_target_issue", lambda issue: "115000101")
    monkeypatch.setattr(
        recommendation_center,
        "save_recommendation_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview must not persist")),
    )
    monkeypatch.setattr(
        recommendation_center,
        "_record_recommendation_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview must not record formal events")),
    )

    result = recommendation_center.generate_recommendation_center(persist=False, calculate_only=True)

    assert result["persisted"] is False
    assert result["saved"]["reason"] == "preview_only"


def test_persist_rejects_insufficient_recommendation(monkeypatch):
    events = []
    monkeypatch.setattr(
        recommendation_center,
        "calculate_recommendation",
        lambda *args, **kwargs: {
            "status": "ok",
            "recommendation": {
                "issue": "115000100",
                "results": [{"numbers": [1, 2, 3]}],
                "recommendation_output": {
                    "is_valid": False,
                    "input_count": 3,
                    "output_count": 3,
                    "model_count": 1,
                    "removed_count": 0,
                    "reason": "recommendation_insufficient",
                },
            },
            "persisted": False,
        },
    )
    monkeypatch.setattr(recommendation_center, "_latest_issue", lambda: "115000100")
    monkeypatch.setattr(recommendation_center, "_target_issue", lambda issue: "115000101")
    monkeypatch.setattr(recommendation_center, "_record_recommendation_event", lambda *args: events.append(args))
    monkeypatch.setattr(
        recommendation_center,
        "save_recommendation_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("insufficient output must not persist")),
    )

    result = recommendation_center.generate_recommendation_center(persist=True)

    assert result["status"] == "skipped"
    assert result["saved"]["reason"] == "recommendation_insufficient"
    assert events[0][0] == "recommendation_insufficient"


def test_persist_valid_recommendation_registers_tracker_without_simulation_scope(monkeypatch):
    import services.learning_engine as learning_engine
    import services.prediction_tracker as prediction_tracker

    tracker_calls = []
    monkeypatch.setattr(recommendation_center, "_latest_issue", lambda: "115000100")
    monkeypatch.setattr(recommendation_center, "_target_issue", lambda issue: "115000101")
    monkeypatch.setattr(recommendation_center, "_record_recommendation_event", lambda *args: None)
    monkeypatch.setattr(
        recommendation_center,
        "calculate_recommendation",
        lambda *args, **kwargs: {
            "status": "ok",
            "recommendation": {
                "issue": "115000100",
                "target_issue": "115000101",
                "results": [{"numbers": list(range(1, 21))}],
                "recommendation_output": {"is_valid": True, "output_count": 20},
            },
            "persisted": False,
        },
    )
    monkeypatch.setattr(recommendation_center, "save_recommendation_run", lambda *args, **kwargs: {"status": "ok", "run_id": 7})
    monkeypatch.setattr(learning_engine, "save_live_prediction_snapshot", lambda payload: {"status": "ok"})
    monkeypatch.setattr(
        prediction_tracker,
        "register_recommendation_prediction",
        lambda recommendation, recommendation_run_id, simulation_run_id=None: tracker_calls.append(
            (recommendation_run_id, simulation_run_id)
        ) or {"status": "ok"},
    )

    result = recommendation_center.generate_recommendation_center(persist=True)

    assert result["status"] == "ok"
    assert result["persisted"] is True
    assert tracker_calls == [(7, None)]
