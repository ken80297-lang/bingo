import sys
import types

from desktop.core.data_repository import DataRepository


def _install_fake_backend(monkeypatch):
    database = types.ModuleType("database")
    official = types.ModuleType("database.official_draw_store")
    prediction = types.ModuleType("database.prediction_history_store")
    analysis = types.ModuleType("database.analysis_store")
    rules = types.ModuleType("database.rule_snapshot_store")
    learning = types.ModuleType("database.learning_store")

    draw = {"issue": "114000002", "numbers": list(range(1, 21)), "super_number": 8, "source": "taiwan_lottery", "verified": True}
    pred = {"issue": "114000001", "prediction_issue": "114000002", "recommend_numbers": list(range(1, 21)), "strategy": "prod"}

    official.get_latest_official_draw = lambda: draw
    official.get_official_draw_history = lambda limit=30: [draw]
    official.get_official_draw_by_issue = lambda issue: draw if issue == "114000002" else None
    prediction.get_latest_prediction_history = lambda: pred
    prediction.get_prediction_for_source_target = lambda source, target: pred if (source, target) == ("114000001", "114000002") else None
    prediction.get_prediction_history_records = lambda limit=100: [pred]
    prediction.get_prediction_history_statistics = lambda limit=100: {"sample_size": 1, "average_hits": 5}
    analysis.get_latest_analysis_history = lambda: {"issue": "114000002"}
    analysis.get_analysis_history_by_issue = lambda issue: {"issue": issue}
    rules.get_rule_snapshot = lambda **kwargs: {"source_issue": kwargs.get("source_issue"), "snapshot_json": {"rules": []}}
    rules.get_rule_snapshots = lambda limit=100: []
    rules.get_latest_rule_snapshot = lambda: {"source_issue": "114000001"}
    learning.get_learning_status_counts = lambda: {"total_records": 1}
    learning.get_learning_model_performance = lambda window=100: []
    learning.get_learning_records = lambda limit=100: []

    for module in [database, official, prediction, analysis, rules, learning]:
        monkeypatch.setitem(sys.modules, module.__name__, module)


def test_repository_reads_backend_without_writes(monkeypatch):
    _install_fake_backend(monkeypatch)
    repo = DataRepository()

    assert repo.get_latest_draw()["issue"] == "114000002"
    assert repo.get_latest_prediction()["prediction_issue"] == "114000002"
    assert repo.get_prediction_for_issue("114000002")["issue"] == "114000001"
    assert repo.block_database_write("x")["status"] == "blocked"

