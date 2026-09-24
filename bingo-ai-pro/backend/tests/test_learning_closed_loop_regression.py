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


def test_fast_path_learning_capture_does_not_change_production_numbers(monkeypatch):
    from services import recommendation_center

    analysis = {
        "issue": "115099900",
        "numbers": list(range(1, 21)),
        "super_number": 1,
    }
    expected_numbers = list(range(21, 41))
    monkeypatch.setattr(recommendation_center, "get_latest_analysis_history", lambda: analysis)
    monkeypatch.setattr(
        recommendation_center,
        "_build_fast_path_numbers",
        lambda *args, **kwargs: (list(expected_numbers), {"overlap": 0}),
    )

    from services import model_engine
    monkeypatch.setattr(
        model_engine,
        "run_all_models",
        lambda limit=100, draws=None, issue=None: {
            "status": "ok",
            "models": [
                {
                    "model": name,
                    "label": name,
                    "confidence": 99,
                    "reason": "learning-only",
                    "candidate_numbers": list(range(41, 61)),
                }
                for name in ("laowanjia", "hotcold", "missing", "pattern", "balance")
            ],
        },
    )

    result = recommendation_center.calculate_fast_recommendation(
        "115099900",
        "115099901",
        context={},
    )

    assert result["status"] == "ok"
    recommendation = result["recommendation"]
    assert recommendation["results"][0]["numbers"] == expected_numbers
    assert recommendation["production_fast_path"]["candidate_numbers"] == expected_numbers
    assert recommendation["model_voting"]["final_candidates"] != expected_numbers


def _complete_snapshot_records():
    recommendation = _recommendation()
    saved = []
    original_upsert = learning_engine.upsert_learning_record
    return recommendation, saved, original_upsert


def test_full_snapshot_verification_learning_closed_loop(monkeypatch):
    store = {}
    learning_used = []

    monkeypatch.setattr(learning_engine, "_resolve_pending_snapshot", lambda *args: None)
    monkeypatch.setattr(learning_engine, "_analysis_by_issue", lambda issue: {"issue": issue})
    monkeypatch.setattr(learning_engine, "_learning_snapshots_for_issue", lambda issue: list(store.get(str(issue), [])))

    def upsert(row):
        issue = str(row["issue"])
        bucket = store.setdefault(issue, [])
        key = (row.get("model_name"), row.get("top_n"))
        bucket[:] = [existing for existing in bucket if (existing.get("model_name"), existing.get("top_n")) != key]
        bucket.append(dict(row))
        return {"status": "ok", "storage": "cloud", **dict(row)}

    monkeypatch.setattr(learning_engine, "upsert_learning_record", upsert)
    monkeypatch.setattr(
        learning_engine,
        "get_official_draw_by_issue",
        lambda issue, verified_only=False: {"issue": issue, "numbers": list(range(1, 21)), "draw_time": "2026-09-24T00:05:00"},
    )
    monkeypatch.setattr(learning_engine, "record_operation_event", lambda **kwargs: None)
    monkeypatch.setattr(learning_engine, "invalidate_learning_status_cache", lambda: None)

    import database.prediction_history_store as prediction_history_store
    monkeypatch.setattr(
        prediction_history_store,
        "mark_prediction_learning_used",
        lambda issue, used: learning_used.append((str(issue), used)) or {"status": "ok", "storage": "cloud", "updated": 1},
    )

    created = learning_engine.save_live_prediction_snapshot(_recommendation())
    assert created["status"] == "ok"
    assert created["records"] == 18
    pending = store["115099901"]
    assert len(pending) == 18
    assert all(row["learned_status"] == "pending" for row in pending)

    evaluated = learning_engine.evaluate_verified_issue("115099901")
    assert evaluated["status"] == "ok", evaluated
    learned = store["115099901"]
    assert len(learned) == 18
    assert all(row["verification_status"] == "verified" for row in learned)
    assert all(row["learned_status"] == "learned" for row in learned)
    assert all(row["learned_at"] for row in learned)
    assert learning_used == [("115099901", True)]


def test_17_of_18_snapshot_cannot_mark_learning_used(monkeypatch):
    complete = []
    for model in learning_engine.EXPECTED_LIVE_MODELS:
        for top_n in learning_engine.EXPECTED_TOP_N:
            complete.append({
                "issue": "115099901",
                "source_issue": "115099900",
                "target_issue": "115099901",
                "model_name": model,
                "top_n": top_n,
                "predicted_count": top_n,
                "predicted_numbers": list(range(1, top_n + 1)),
                "prediction_snapshot": {"source_issue": "115099900"},
                "analysis_snapshot": {"issue": "115099900"},
                "learned_status": "pending",
                "verification_status": "pending_official",
            })
    partial = complete[:-1]
    learning_used = []
    saved = []

    monkeypatch.setattr(learning_engine, "_learning_snapshots_for_issue", lambda issue: list(partial))
    monkeypatch.setattr(learning_engine, "_latest_prediction_for_issue", lambda issue: None)
    monkeypatch.setattr(learning_engine, "upsert_learning_record", lambda row: saved.append(row) or row)

    import database.prediction_history_store as prediction_history_store
    monkeypatch.setattr(
        prediction_history_store,
        "mark_prediction_learning_used",
        lambda issue, used: learning_used.append((str(issue), used)) or {"status": "ok"},
    )

    result = learning_engine.evaluate_verified_issue("115099901")
    assert result["status"] == "missing_snapshot", result
    assert learning_used == []
    assert len(saved) == 1
    assert saved[0]["learned_status"] == "missing_snapshot"


def test_voting_adaptive_learning_off_vs_on(monkeypatch):
    from services import voting_engine

    models = [
        {"model": "laowanjia", "label": "L", "confidence": 60, "reason": "", "candidate_numbers": list(range(1, 21))},
        {"model": "hotcold", "label": "H", "confidence": 60, "reason": "", "candidate_numbers": list(range(21, 41))},
        {"model": "missing", "label": "M", "confidence": 60, "reason": "", "candidate_numbers": list(range(21, 41))},
        {"model": "pattern", "label": "P", "confidence": 60, "reason": "", "candidate_numbers": list(range(21, 41))},
        {"model": "balance", "label": "B", "confidence": 60, "reason": "", "candidate_numbers": list(range(21, 41))},
    ]
    monkeypatch.setattr(voting_engine, "run_all_models", lambda limit=100: {"status": "ok", "latest_issue": "115099900", "models": models})
    monkeypatch.setattr(voting_engine, "model_hit_rates", lambda limit=100: {name: 0 for name in voting_engine.MODEL_NAMES})

    monkeypatch.setattr(voting_engine, "get_active_adaptive_weights", lambda: None)
    off = voting_engine.build_voting_result(100)
    assert off["adaptive_learning"]["enabled"] is False
    assert off["model_scores"]["laowanjia"]["adaptive_multiplier"] == 1.0

    monkeypatch.setattr(voting_engine, "get_active_adaptive_weights", lambda: {
        "id": 7, "version": 1, "strategy": "v7_models",
        "laowanjia_weight": 1.5, "hot_cold_weight": 0.5,
        "missing_weight": 0.5, "pattern_weight": 0.5, "balance_weight": 0.5,
    })
    on = voting_engine.build_voting_result(100)
    assert on["adaptive_learning"] == {"enabled": True, "weight_id": 7, "version": 1}
    assert on["model_scores"]["laowanjia"]["adaptive_multiplier"] == 1.5
    assert on["model_scores"]["hotcold"]["adaptive_multiplier"] == 0.5
    assert on["model_scores"]["laowanjia"]["effective_vote_weight"] > off["model_scores"]["laowanjia"]["effective_vote_weight"]
    assert on["model_scores"]["hotcold"]["effective_vote_weight"] < off["model_scores"]["hotcold"]["effective_vote_weight"]
    assert on["final_candidates"] != off["final_candidates"]


def test_legacy_adaptive_weights_are_not_applied_to_v7(monkeypatch):
    from services import voting_engine

    monkeypatch.setattr(voting_engine, "run_all_models", lambda limit=100: {
        "status": "ok", "latest_issue": "115099900",
        "models": [{"model": name, "label": name, "confidence": 80, "reason": "", "candidate_numbers": list(range(1, 21))} for name in voting_engine.MODEL_NAMES],
    })
    monkeypatch.setattr(voting_engine, "model_hit_rates", lambda limit=100: {name: 0 for name in voting_engine.MODEL_NAMES})
    monkeypatch.setattr(voting_engine, "get_active_adaptive_weights", lambda: {
        "id": 3, "version": 99, "strategy": "legacy", "laowanjia_weight": 1.5,
    })
    result = voting_engine.build_voting_result(100)
    assert result["adaptive_learning"]["enabled"] is False
    assert all(payload["adaptive_multiplier"] == 1.0 for payload in result["model_scores"].values())


def test_adaptive_updater_requires_all_models_and_minimum_samples(monkeypatch):
    rows = [
        {"model_name": name, "sample_size": 20, "average_hits": 5.0}
        for name in ("laowanjia", "hotcold", "missing", "pattern")
    ]
    monkeypatch.setattr(learning_engine, "get_complete_live_learning_records", lambda window: [])
    saved = []
    monkeypatch.setattr(learning_engine, "save_adaptive_weights", lambda payload: saved.append(payload) or {"status": "ok"})
    result = learning_engine.update_v7_adaptive_weights("115099901")
    assert result["status"] == "skipped"
    assert result["reason"] == "insufficient_complete_targets"
    assert saved == []

    rows.append({"model_name": "balance", "sample_size": 19, "average_hits": 5.0})
    result = learning_engine.update_v7_adaptive_weights("115099901")
    assert result["status"] == "skipped"
    assert result["reason"] == "insufficient_samples"
    assert saved == []


def test_verified_learning_persists_versioned_v7_weights(monkeypatch):
    performance = [
        {"model_name": "laowanjia", "sample_size": 25, "average_hits": 6.0},
        {"model_name": "hotcold", "sample_size": 25, "average_hits": 5.0},
        {"model_name": "missing", "sample_size": 25, "average_hits": 4.0},
        {"model_name": "pattern", "sample_size": 25, "average_hits": 5.0},
        {"model_name": "balance", "sample_size": 25, "average_hits": 5.0},
    ]
    rows = []
    for n in range(25, 0, -1):
        rows.extend(_complete_learning_rows(str(115098000 + n)))
    for row in rows:
        row["hit_count"] = {"laowanjia": 6, "hotcold": 5, "missing": 4, "pattern": 5, "balance": 5, "ensemble": 5}[row["model_name"]]
    monkeypatch.setattr(learning_engine, "get_complete_live_learning_records", lambda window: rows)
    monkeypatch.setattr(learning_engine, "get_latest_adaptive_weights", lambda: {"version": 4})
    saved = []
    monkeypatch.setattr(learning_engine, "save_adaptive_weights", lambda payload: saved.append(dict(payload)) or {"status": "ok", "storage": "cloud", "weight_id": 9})
    monkeypatch.setattr(learning_engine, "mark_learning_weight_changed", lambda issue, changed=True: {"status": "ok", "storage": "cloud", "updated": 18})
    result = learning_engine.update_v7_adaptive_weights("115099901")
    assert result["status"] == "ok"
    assert result["version"] == 5
    assert len(saved) == 1
    payload = saved[0]
    assert payload["strategy"] == "v7_models"
    assert payload["laowanjia_weight"] > payload["hot_cold_weight"] > payload["missing_weight"]
    assert abs(sum(result["weights"].values()) - 5.0) < 0.00001


def test_17_of_18_never_invokes_adaptive_updater(monkeypatch):
    complete = []
    for model in learning_engine.EXPECTED_LIVE_MODELS:
        for top_n in learning_engine.EXPECTED_TOP_N:
            complete.append({
                "issue": "115099901", "source_issue": "115099900", "target_issue": "115099901",
                "model_name": model, "top_n": top_n, "predicted_count": top_n,
                "predicted_numbers": list(range(1, top_n + 1)),
                "prediction_snapshot": {"source_issue": "115099900"},
                "analysis_snapshot": {"issue": "115099900"},
                "learned_status": "pending", "verification_status": "pending_official",
            })
    monkeypatch.setattr(learning_engine, "_learning_snapshots_for_issue", lambda issue: complete[:-1])
    monkeypatch.setattr(learning_engine, "_latest_prediction_for_issue", lambda issue: None)
    monkeypatch.setattr(learning_engine, "upsert_learning_record", lambda row: row)
    adaptive_calls = []
    monkeypatch.setattr(learning_engine, "update_v7_adaptive_weights", lambda issue: adaptive_calls.append(issue) or {"status": "ok"})
    result = learning_engine.evaluate_verified_issue("115099901")
    assert result["status"] == "missing_snapshot"
    assert adaptive_calls == []


def _complete_learning_rows(issue: str):
    rows = []
    for model in learning_engine.EXPECTED_LIVE_MODELS:
        for top_n in learning_engine.EXPECTED_TOP_N:
            rows.append({
                "issue": issue,
                "model_name": model,
                "top_n": top_n,
                "prediction_type": "live_prediction",
                "verification_status": "verified",
                "learned_status": "learned",
                "hit_count": 5,
            })
    return rows


def test_complete_learning_window_pages_to_100_targets_across_500_boundary(monkeypatch):
    from database import learning_store

    # 100 complete targets = 1,800 rows. 500-row pages deliberately split
    # targets because 500 is not divisible by 18.
    source = []
    for n in range(100, 0, -1):
        source.extend(_complete_learning_rows(str(115100000 + n)))

    calls = []
    def fake_get_learning_records(limit=100, offset=0, **kwargs):
        calls.append((limit, offset, kwargs))
        return source[offset: offset + limit]

    monkeypatch.setattr(learning_store, "get_learning_records", fake_get_learning_records)
    result = learning_store.get_complete_live_learning_records(window=100, page_size=500, max_rows=2500)

    assert len(result) == 1800
    assert len({row["issue"] for row in result}) == 100
    assert [offset for _, offset, _ in calls] == [0, 500, 1000, 1500]
    assert all(call[2]["prediction_type"] == "live_prediction" for call in calls)
    assert all(call[2]["verification_status"] == "verified" for call in calls)
    assert all(call[2]["learned_status"] == "learned" for call in calls)


def test_complete_learning_window_excludes_incomplete_target(monkeypatch):
    from database import learning_store

    newest_incomplete = _complete_learning_rows("115200003")[:-1]
    older_complete = _complete_learning_rows("115200002") + _complete_learning_rows("115200001")
    source = newest_incomplete + older_complete

    monkeypatch.setattr(
        learning_store,
        "get_learning_records",
        lambda limit=100, offset=0, **kwargs: source[offset: offset + limit],
    )
    result = learning_store.get_complete_live_learning_records(window=2, page_size=20, max_rows=200)

    assert len(result) == 36
    assert {row["issue"] for row in result} == {"115200002", "115200001"}


def test_adaptive_updater_uses_full_100_complete_target_window(monkeypatch):
    rows = []
    for n in range(100, 0, -1):
        rows.extend(_complete_learning_rows(str(115300000 + n)))

    monkeypatch.setattr(learning_engine, "get_adaptive_weights_by_source_issue", lambda issue: None)
    monkeypatch.setattr(learning_engine, "get_complete_live_learning_records", lambda window: rows)
    monkeypatch.setattr(learning_engine, "get_latest_adaptive_weights", lambda: {"version": 7})
    saved = []
    monkeypatch.setattr(
        learning_engine,
        "save_adaptive_weights",
        lambda payload: saved.append(dict(payload)) or {"status": "ok", "storage": "cloud", "weight_id": 10},
    )

    result = learning_engine.update_v7_adaptive_weights("115300101")
    assert result["status"] == "ok"
    assert result["complete_targets"] == 100
    assert result["version"] == 8
    assert saved[0]["window"] == 100


def test_adaptive_weight_change_requires_exact_18_cloud_evidence(monkeypatch):
    rows = []
    for n in range(20, 0, -1):
        rows.extend(_complete_learning_rows(str(115400000 + n)))
    monkeypatch.setattr(learning_engine, "get_adaptive_weights_by_source_issue", lambda issue: None)
    monkeypatch.setattr(learning_engine, "get_complete_live_learning_records", lambda window: rows)
    monkeypatch.setattr(learning_engine, "get_latest_adaptive_weights", lambda: {"version": 1})
    monkeypatch.setattr(learning_engine, "save_adaptive_weights", lambda payload: {"status": "ok", "storage": "cloud", "weight_id": 2})

    monkeypatch.setattr(learning_engine, "mark_learning_weight_changed", lambda issue, changed=True: {"status": "ok", "storage": "cloud", "updated": 17})
    result = learning_engine.update_v7_adaptive_weights("115400021")
    assert result["status"] == "error"
    assert result["reason"] == "adaptive_weight_evidence_update_required"

    monkeypatch.setattr(learning_engine, "mark_learning_weight_changed", lambda issue, changed=True: {"status": "ok", "storage": "sqlite", "updated": 18})
    result = learning_engine.update_v7_adaptive_weights("115400021")
    assert result["status"] == "error"

    monkeypatch.setattr(learning_engine, "mark_learning_weight_changed", lambda issue, changed=True: {"status": "ok", "storage": "cloud", "updated": 18})
    result = learning_engine.update_v7_adaptive_weights("115400021")
    assert result["status"] == "ok"
    assert result["evidence"]["updated"] == 18


def test_existing_adaptive_weight_reconciles_interrupted_evidence(monkeypatch):
    monkeypatch.setattr(
        learning_engine,
        "get_adaptive_weights_by_source_issue",
        lambda issue: {"id": 9, "version": 4, "strategy": "v7_models"},
    )
    calls = []
    monkeypatch.setattr(
        learning_engine,
        "mark_learning_weight_changed",
        lambda issue, changed=True: calls.append((issue, changed)) or {"status": "ok", "storage": "cloud", "updated": 18},
    )
    result = learning_engine.update_v7_adaptive_weights("115500001")
    assert result["status"] == "ok"
    assert result["reason"] == "already_updated"
    assert calls == [("115500001", True)]
    assert result["evidence"]["updated"] == 18


def test_existing_adaptive_weight_does_not_hide_failed_reconciliation(monkeypatch):
    monkeypatch.setattr(
        learning_engine,
        "get_adaptive_weights_by_source_issue",
        lambda issue: {"id": 9, "version": 4, "strategy": "v7_models"},
    )
    monkeypatch.setattr(
        learning_engine,
        "mark_learning_weight_changed",
        lambda issue, changed=True: {"status": "ok", "storage": "cloud", "updated": 17},
    )
    result = learning_engine.update_v7_adaptive_weights("115500001")
    assert result["status"] == "error"
    assert result["reason"] == "adaptive_weight_evidence_reconciliation_required"


def test_complete_verified_issue_invokes_adaptive_updater_once(monkeypatch):
    complete = _complete_learning_rows("115600001")
    for row in complete:
        row.update({
            "source_issue": "115600000",
            "target_issue": "115600001",
            "predicted_numbers": list(range(1, int(row["top_n"]) + 1)),
            "prediction_snapshot": {"source_issue": "115600000"},
            "analysis_snapshot": {"issue": "115600000"},
        })
    monkeypatch.setattr(learning_engine, "capture_prediction_snapshot", lambda issue: {"status": "ok", "learning_records": complete})
    monkeypatch.setattr(learning_engine, "get_official_draw_by_issue", lambda issue, verified_only=False: {"issue": issue, "numbers": list(range(1, 21)), "draw_time": "x"})
    monkeypatch.setattr(learning_engine, "upsert_learning_record", lambda row: {"status": "ok", "storage": "cloud"})
    monkeypatch.setattr(learning_engine, "record_operation_event", lambda **kwargs: None)
    adaptive = []
    monkeypatch.setattr(learning_engine, "update_v7_adaptive_weights", lambda issue: adaptive.append(issue) or {"status": "ok"})
    import database.prediction_history_store as phs
    monkeypatch.setattr(phs, "mark_prediction_learning_used", lambda issue, used: {"status": "ok", "storage": "cloud", "updated": 1})

    result = learning_engine.evaluate_verified_issue("115600001")
    assert result["status"] == "ok"
    assert result["records"] == 18
    assert adaptive == ["115600001"]
    assert result["adaptive_weights"]["status"] == "ok"


def test_active_v7_weights_require_complete_weight_changed_evidence(monkeypatch):
    from database import adaptive_weight_store

    captured = {}
    def fake_query(sql, params=(), sqlite_sql=None):
        captured["cloud"] = sql
        captured["sqlite"] = sqlite_sql
        return []

    monkeypatch.setattr(adaptive_weight_store, "_query_with_fallback", fake_query)
    assert adaptive_weight_store.get_active_adaptive_weights() is None

    cloud = captured["cloud"]
    sqlite = captured["sqlite"]
    assert "aw.strategy <> 'v7_models'" in cloud
    assert "lh.prediction_type = 'live_prediction'" in cloud
    assert "lh.verification_status = 'verified'" in cloud
    assert "lh.learned_status = 'learned'" in cloud
    assert "lh.weight_changed = true" in cloud
    assert "count(distinct (lh.model_name, lh.top_n))" in cloud
    assert "lh.model_name in ('laowanjia','hotcold','missing','pattern','balance','ensemble')" in cloud
    assert "lh.top_n in (5,10,20)" in cloud
    assert ") = 18" in cloud
    assert "lh.weight_changed = 1" in sqlite
    assert "count(distinct lh.model_name || ':' || cast(lh.top_n as text))" in sqlite
    assert "lh.model_name in ('laowanjia','hotcold','missing','pattern','balance','ensemble')" in sqlite
    assert "lh.top_n in (5,10,20)" in sqlite
    assert ") = 18" in sqlite


def test_balance_model_always_emits_twenty_unique_candidates():
    from services.model_engine import model_e_balance

    draws = [
        {"issue": str(1000 - i), "numbers": list(range(1 + (i % 4), 81, 4))[:20]}
        for i in range(30)
    ]
    result = model_e_balance(draws)
    candidates = result["candidate_numbers"]
    assert len(candidates) == 20
    assert len(set(candidates)) == 20
    assert all(1 <= number <= 80 for number in candidates)


def _setup_complete_verified_gate_case(monkeypatch, save_results, adaptive_result):
    complete = _complete_learning_rows("115700001")
    for row in complete:
        row.update({
            "source_issue": "115700000",
            "target_issue": "115700001",
            "predicted_numbers": list(range(1, int(row["top_n"]) + 1)),
            "prediction_snapshot": {"source_issue": "115700000"},
            "analysis_snapshot": {"issue": "115700000"},
        })
    monkeypatch.setattr(learning_engine, "capture_prediction_snapshot", lambda issue: {"status": "ok", "learning_records": complete})
    monkeypatch.setattr(learning_engine, "get_official_draw_by_issue", lambda issue, verified_only=False: {"issue": issue, "numbers": list(range(1, 21)), "draw_time": "x"})
    results = iter(save_results)
    monkeypatch.setattr(learning_engine, "upsert_learning_record", lambda row: next(results))
    monkeypatch.setattr(learning_engine, "record_operation_event", lambda **kwargs: None)
    adaptive_calls = []
    monkeypatch.setattr(learning_engine, "update_v7_adaptive_weights", lambda issue: adaptive_calls.append(issue) or dict(adaptive_result))
    import database.prediction_history_store as phs
    learning_used = []
    monkeypatch.setattr(phs, "mark_prediction_learning_used", lambda issue, used: learning_used.append((issue, used)) or {"status": "ok", "storage": "cloud", "updated": 1})
    return adaptive_calls, learning_used


def test_verified_learning_rejects_one_sqlite_fallback(monkeypatch):
    saves = [{"status": "ok", "storage": "cloud"} for _ in range(17)] + [{"status": "ok", "storage": "sqlite"}]
    adaptive_calls, learning_used = _setup_complete_verified_gate_case(monkeypatch, saves, {"status": "ok"})
    result = learning_engine.evaluate_verified_issue("115700001")
    assert result["status"] == "error"
    assert result["reason"] == "learning_cloud_save_required"
    assert adaptive_calls == []
    assert learning_used == []


def test_verified_learning_requires_storage_cloud_field(monkeypatch):
    saves = [{"status": "ok", "storage": "cloud"} for _ in range(17)] + [{"status": "ok"}]
    adaptive_calls, learning_used = _setup_complete_verified_gate_case(monkeypatch, saves, {"status": "ok"})
    result = learning_engine.evaluate_verified_issue("115700001")
    assert result["status"] == "error"
    assert result["reason"] == "learning_cloud_save_required"
    assert adaptive_calls == []
    assert learning_used == []


def test_verified_learning_adaptive_error_never_marks_learning_used(monkeypatch):
    saves = [{"status": "ok", "storage": "cloud"} for _ in range(18)]
    adaptive_calls, learning_used = _setup_complete_verified_gate_case(monkeypatch, saves, {"status": "error", "reason": "evidence_failed"})
    result = learning_engine.evaluate_verified_issue("115700001")
    assert result["status"] == "error"
    assert result["reason"] == "adaptive_learning_failed"
    assert adaptive_calls == ["115700001"]
    assert learning_used == []


def test_verified_learning_adaptive_skipped_allows_learning_used(monkeypatch):
    saves = [{"status": "ok", "storage": "cloud"} for _ in range(18)]
    adaptive_calls, learning_used = _setup_complete_verified_gate_case(monkeypatch, saves, {"status": "skipped", "reason": "insufficient_samples"})
    result = learning_engine.evaluate_verified_issue("115700001")
    assert result["status"] == "ok"
    assert adaptive_calls == ["115700001"]
    assert learning_used == [("115700001", True)]


def test_verified_learning_rejects_sqlite_learning_used_update(monkeypatch):
    saves = [{"status": "ok", "storage": "cloud"} for _ in range(18)]
    adaptive_calls, _ = _setup_complete_verified_gate_case(monkeypatch, saves, {"status": "skipped", "reason": "insufficient_samples"})
    import database.prediction_history_store as phs
    monkeypatch.setattr(phs, "mark_prediction_learning_used", lambda issue, used: {"status": "ok", "storage": "sqlite", "updated": 1})
    result = learning_engine.evaluate_verified_issue("115700001")
    assert result["status"] == "error"
    assert result["reason"] == "learning_used_cloud_update_required"
    assert adaptive_calls == ["115700001"]


def test_verified_learning_rejects_zero_cloud_learning_used_updates(monkeypatch):
    saves = [{"status": "ok", "storage": "cloud"} for _ in range(18)]
    adaptive_calls, _ = _setup_complete_verified_gate_case(monkeypatch, saves, {"status": "skipped", "reason": "insufficient_samples"})
    import database.prediction_history_store as phs
    monkeypatch.setattr(phs, "mark_prediction_learning_used", lambda issue, used: {"status": "ok", "storage": "cloud", "updated": 0})
    result = learning_engine.evaluate_verified_issue("115700001")
    assert result["status"] == "error"
    assert result["reason"] == "learning_used_cloud_update_required"
    assert adaptive_calls == ["115700001"]
