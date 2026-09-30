from services import learning_evidence


def test_learning_evidence_requires_durable_closed_loop(monkeypatch):
    rows = [
        {
            "id": 2, "version": 2, "strategy": "v7_models", "window": 20,
            "source_evaluation_id": 115000002, "laowanjia_weight": 1.1,
            "hot_cold_weight": 1.0, "missing_weight": 0.9,
            "pattern_weight": 1.0, "balance_weight": 1.0,
        },
        {
            "id": 1, "version": 1, "strategy": "v7_models", "window": 20,
            "source_evaluation_id": 115000001, "laowanjia_weight": 1.0,
            "hot_cold_weight": 1.0, "missing_weight": 1.0,
            "pattern_weight": 1.0, "balance_weight": 1.0,
        },
    ]
    monkeypatch.setattr(learning_evidence, "get_adaptive_weight_history", lambda limit: rows)
    ledger = [
        {"model_name": model, "top_n": top_n, "weight_changed": True}
        for model in ("laowanjia", "hotcold", "missing", "pattern", "balance", "ensemble")
        for top_n in (5, 10, 20)
    ]
    monkeypatch.setattr(learning_evidence, "get_learning_records", lambda **kwargs: ledger)
    monkeypatch.setattr(
        learning_evidence,
        "get_prediction_history_records",
        lambda limit: [{"prediction_issue": "115000002", "prediction_status": "verified", "learning_used": True}],
    )

    result = learning_evidence.get_learning_evidence(1)
    event = result["events"][0]
    assert result["latest_proven"] is True
    assert event["learning_ledger_rows"] == 18
    assert event["weights_actually_changed"] is True
    assert event["weight_delta"]["laowanjia"] == 0.1
    assert event["weight_delta"]["missing"] == -0.1


def test_learning_evidence_rejects_ui_only_claim(monkeypatch):
    monkeypatch.setattr(
        learning_evidence,
        "get_adaptive_weight_history",
        lambda limit: [
            {"id": 2, "version": 2, "strategy": "v7_models", "source_evaluation_id": 115000002,
             "laowanjia_weight": 1.1, "hot_cold_weight": 1.0, "missing_weight": 0.9,
             "pattern_weight": 1.0, "balance_weight": 1.0},
            {"id": 1, "version": 1, "strategy": "v7_models", "source_evaluation_id": 115000001,
             "laowanjia_weight": 1.0, "hot_cold_weight": 1.0, "missing_weight": 1.0,
             "pattern_weight": 1.0, "balance_weight": 1.0},
        ],
    )
    monkeypatch.setattr(learning_evidence, "get_learning_records", lambda **kwargs: [])
    monkeypatch.setattr(learning_evidence, "get_prediction_history_records", lambda limit: [])

    result = learning_evidence.get_learning_evidence(1)
    assert result["latest_proven"] is False
    assert result["events"][0]["proven"] is False
