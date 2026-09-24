from services import learning_engine


def _model_scores():
    return {
        name: {
            "confidence": 70,
            "candidate_numbers": list(range(offset, offset + 20)),
        }
        for name, offset in {
            "laowanjia": 1,
            "hotcold": 11,
            "missing": 21,
            "pattern": 31,
            "balance": 41,
        }.items()
    }


def _recommendation():
    scores = _model_scores()
    return {
        "issue": "115099900",
        "target_issue": "115099901",
        "created_at": "2026-09-24T00:00:00",
        "confidence": 80,
        "model_scores": scores,
        "model_voting": {
            "status": "ok",
            "model_scores": scores,
            "final_candidates": list(range(1, 21)),
            "winning_model": "laowanjia",
            "confidence": 75,
        },
        "results": [{"numbers": list(range(1, 21))}],
    }


def test_live_snapshot_writes_exactly_18_model_topn_records(monkeypatch):
    saved = []
    monkeypatch.setattr(learning_engine, "_resolve_pending_snapshot", lambda *args: None)
    monkeypatch.setattr(learning_engine, "_learning_snapshots_for_issue", lambda issue: [])
    monkeypatch.setattr(learning_engine, "_analysis_by_issue", lambda issue: {"issue": issue})
    monkeypatch.setattr(learning_engine, "upsert_learning_record", lambda row: saved.append(row) or row)

    result = learning_engine.save_live_prediction_snapshot(_recommendation())

    assert result["status"] == "ok"
    assert result["records"] == 18
    assert len(saved) == 18
    assert {row["model_name"] for row in saved} == {
        "laowanjia", "hotcold", "missing", "pattern", "balance", "ensemble"
    }
    assert {row["top_n"] for row in saved} == {5, 10, 20}
    assert all(row["model_name"] != "unknown" for row in saved)
    assert all(row["prediction_snapshot"] for row in saved)


def test_incomplete_unknown_marker_does_not_block_snapshot_rebuild(monkeypatch):
    saved = []
    marker = {
        "model_name": "unknown",
        "top_n": 0,
        "predicted_count": 0,
        "predicted_numbers": [],
        "prediction_snapshot": {},
    }
    monkeypatch.setattr(learning_engine, "_resolve_pending_snapshot", lambda *args: None)
    monkeypatch.setattr(learning_engine, "_learning_snapshots_for_issue", lambda issue: [marker])
    monkeypatch.setattr(learning_engine, "_analysis_by_issue", lambda issue: {"issue": issue})
    monkeypatch.setattr(learning_engine, "upsert_learning_record", lambda row: saved.append(row) or row)

    result = learning_engine.save_live_prediction_snapshot(_recommendation())

    assert result["status"] == "ok"
    assert not result.get("skipped", False)
    assert result["records"] == 18
    assert len(saved) == 18


def test_complete_18_record_snapshot_is_idempotent(monkeypatch):
    existing = []
    for model in learning_engine.EXPECTED_LIVE_MODELS:
        for top_n in learning_engine.EXPECTED_TOP_N:
            existing.append({
                "model_name": model,
                "top_n": top_n,
                "predicted_count": top_n,
                "predicted_numbers": list(range(1, top_n + 1)),
                "prediction_snapshot": {"source_issue": "115099900"},
                "history_cutoff_issue": "115099900",
                "prediction_created_at": "2026-09-24T00:00:00",
            })
    monkeypatch.setattr(learning_engine, "_resolve_pending_snapshot", lambda *args: None)
    monkeypatch.setattr(learning_engine, "_learning_snapshots_for_issue", lambda issue: existing)

    result = learning_engine.save_live_prediction_snapshot(_recommendation())

    assert result["status"] == "ok"
    assert result["skipped"] is True
    assert result["records"] == 18


def test_capture_rejects_partial_snapshot(monkeypatch):
    partial = [{
        "model_name": "laowanjia",
        "top_n": 5,
        "predicted_count": 5,
        "predicted_numbers": [1, 2, 3, 4, 5],
        "prediction_snapshot": {"source_issue": "115099900"},
        "analysis_snapshot": {"issue": "115099900"},
    }]
    monkeypatch.setattr(learning_engine, "_learning_snapshots_for_issue", lambda issue: partial)

    result = learning_engine.capture_prediction_snapshot("115099901")

    assert result["status"] == "missing_snapshot"
    assert result["learning_records"] == []


def test_capture_accepts_only_complete_18_record_snapshot(monkeypatch):
    complete = []
    for model in learning_engine.EXPECTED_LIVE_MODELS:
        for top_n in learning_engine.EXPECTED_TOP_N:
            complete.append({
                "model_name": model,
                "top_n": top_n,
                "predicted_count": top_n,
                "predicted_numbers": list(range(1, top_n + 1)),
                "prediction_snapshot": {"source_issue": "115099900"},
                "analysis_snapshot": {"issue": "115099900"},
            })
    monkeypatch.setattr(learning_engine, "_learning_snapshots_for_issue", lambda issue: complete)

    result = learning_engine.capture_prediction_snapshot("115099901")

    assert result["status"] == "ok"
    assert len(result["learning_records"]) == 18


def test_prediction_history_recovery_rejects_fast_path_only_row():
    prediction = {
        "issue": "115099900",
        "prediction_issue": "115099901",
        "predict_time": "2026-09-24T00:00:00",
        "recommend_numbers": list(range(1, 21)),
        "model_scores": {
            "production_fast_path": {
                "candidate_numbers": list(range(1, 21)),
                "confidence": 80,
            }
        },
    }
    official = {"numbers": list(range(1, 21)), "draw_time": "2026-09-24T00:05:00"}

    records = learning_engine._learning_records_from_prediction(prediction, official, {})

    assert records == []


def test_prediction_history_recovery_requires_and_builds_18_records():
    prediction = {
        "issue": "115099900",
        "prediction_issue": "115099901",
        "predict_time": "2026-09-24T00:00:00",
        "recommend_numbers": list(range(1, 21)),
        "model_scores": _model_scores(),
    }
    official = {"numbers": list(range(1, 21)), "draw_time": "2026-09-24T00:05:00"}

    records = learning_engine._learning_records_from_prediction(prediction, official, {})

    assert len(records) == 18
    assert {row["model_name"] for row in records} == {
        "laowanjia", "hotcold", "missing", "pattern", "balance", "ensemble"
    }
    assert {row["top_n"] for row in records} == {5, 10, 20}


def test_prediction_service_persists_learning_snapshot_once(monkeypatch):
    from services import prediction_service

    monkeypatch.setattr(prediction_service, "_acquire_prediction_lock", lambda owner: (True, {"status": "ok", "lock_token": "t"}))
    monkeypatch.setattr(prediction_service, "_release_prediction_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(prediction_service, "_existing_prediction", lambda *args, **kwargs: None)
    monkeypatch.setattr(prediction_service, "_record_event", lambda **kwargs: None)
    monkeypatch.setattr(prediction_service, "get_production_generation", lambda: 2)
    monkeypatch.setattr(prediction_service, "PREDICTION_TIMEOUT_SECONDS", 999.0)
    monkeypatch.setattr(prediction_service, "_duration_ms", lambda start: 0.0)
    monkeypatch.setattr(prediction_service, "is_issue_in_current_generation", lambda issue: True)

    recommendation = {
        "issue": "115000001",
        "target_issue": "115000002",
        "recommended_numbers": list(range(1, 21)),
        "recommend_numbers": list(range(1, 21)),
        "model_scores": {},
    }
    monkeypatch.setattr(
        prediction_service,
        "calculate_fast_recommendation",
        lambda *args, **kwargs: {"status": "ok", "recommendation": dict(recommendation)},
    )
    monkeypatch.setattr(
        prediction_service,
        "build_prediction_history_record",
        lambda rec: {"recommend_numbers": list(range(1, 21)), "model_scores": {}},
    )
    monkeypatch.setattr(
        prediction_service,
        "save_prediction_history",
        lambda *args, **kwargs: {"status": "ok", "id": 99, "storage": "test"},
    )

    calls = []
    monkeypatch.setattr(
        learning_engine,
        "save_live_prediction_snapshot",
        lambda rec: calls.append(dict(rec)) or {"status": "ok", "records": 18},
    )

    result = prediction_service.create_for_official_draw(
        "115000001",
        source="test",
        trigger="regression",
        target_issue="115000002",
    )

    assert result["status"] == "created", result
    assert result["learning_snapshot_complete"] is True
    assert result["learning_snapshot_warning"] is None
    assert len(calls) == 1
    assert calls[0]["issue"] == "115000001"
    assert calls[0]["target_issue"] == "115000002"


def test_prediction_service_exposes_incomplete_learning_snapshot(monkeypatch):
    from services import prediction_service

    monkeypatch.setattr(prediction_service, "_acquire_prediction_lock", lambda owner: (True, {"status": "ok", "lock_token": "t"}))
    monkeypatch.setattr(prediction_service, "_release_prediction_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(prediction_service, "_existing_prediction", lambda *args, **kwargs: None)
    monkeypatch.setattr(prediction_service, "_record_event", lambda **kwargs: None)
    monkeypatch.setattr(prediction_service, "get_production_generation", lambda: 2)
    monkeypatch.setattr(prediction_service, "PREDICTION_TIMEOUT_SECONDS", 999.0)
    monkeypatch.setattr(prediction_service, "_duration_ms", lambda start: 0.0)
    monkeypatch.setattr(prediction_service, "is_issue_in_current_generation", lambda issue: True)
    monkeypatch.setattr(
        prediction_service,
        "calculate_fast_recommendation",
        lambda *args, **kwargs: {
            "status": "ok",
            "recommendation": {
                "issue": "115000001",
                "target_issue": "115000002",
                "recommended_numbers": list(range(1, 21)),
                "recommend_numbers": list(range(1, 21)),
                "model_scores": {},
            },
        },
    )
    monkeypatch.setattr(
        prediction_service,
        "build_prediction_history_record",
        lambda rec: {"recommend_numbers": list(range(1, 21)), "model_scores": {}},
    )
    monkeypatch.setattr(
        prediction_service,
        "save_prediction_history",
        lambda *args, **kwargs: {"status": "ok", "id": 100, "storage": "test"},
    )
    monkeypatch.setattr(
        learning_engine,
        "save_live_prediction_snapshot",
        lambda rec: {"status": "ok", "records": 3},
    )

    result = prediction_service.create_for_official_draw(
        "115000001",
        source="test",
        trigger="regression",
        target_issue="115000002",
    )

    assert result["status"] == "created", result
    assert result["persisted"] is True
    assert result["learning_snapshot_complete"] is False
    assert result["learning_snapshot_warning"] == "learning_snapshot_incomplete"
