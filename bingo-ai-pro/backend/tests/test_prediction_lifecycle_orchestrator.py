from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import prediction_lifecycle_orchestrator as orchestrator


def setup_function():
    orchestrator.shutdown_lifecycle_background_tasks(wait=True)
    orchestrator.reset_lifecycle_background_tasks_for_tests()


def teardown_function():
    orchestrator.shutdown_lifecycle_background_tasks(wait=True)


def test_process_official_draw_lifecycle_verifies_learns_and_creates_next(monkeypatch):
    events = []

    monkeypatch.setattr(
        orchestrator,
        "verify_prediction",
        lambda draw: {"status": "ok", "updated": 1, "issue": draw["issue"], "prediction_status": "verified"},
    )

    def fake_import(name, *args, **kwargs):
        if name == "services.learning_engine":
            class Learning:
                @staticmethod
                def evaluate_verified_issue(issue):
                    return {"status": "ok", "issue": issue, "learned": True}

            return Learning
        if name == "database.analysis_store":
            class AnalysisStore:
                @staticmethod
                def save_analysis_history(draw):
                    return {"status": "ok", "issue": draw["issue"]}

            return AnalysisStore
        if name == "services.operations_center":
            class Operations:
                @staticmethod
                def record_operation_event(**kwargs):
                    events.append(kwargs)

            return Operations
        return real_import(name, *args, **kwargs)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr(
        orchestrator,
        "refresh_next_prediction_for_draw",
        lambda draw: {"status": "created", "based_on_issue": draw["issue"], "target_issue": "100000003"},
    )

    result = orchestrator.process_official_draw_lifecycle(
        {"issue": "100000002", "numbers": list(range(1, 21)), "super_number": 7},
        caller="unit_test",
        learning_synchronous=True,
    )

    assert result["status"] == "ok"
    assert result["verification"]["prediction_status"] == "verified"
    assert result["analysis"]["status"] == "ok"
    assert result["learning"]["learned"] is True
    assert result["prediction"]["target_issue"] == "100000003"
    assert [event["event_type"] for event in events] == [
        "official_draw_lifecycle_started",
        "official_draw_lifecycle_completed",
    ]


def test_process_official_draw_lifecycle_can_disable_next_prediction(monkeypatch):
    monkeypatch.setattr(orchestrator, "_record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "verify_prediction", lambda draw: {"status": "ok", "updated": 1})

    def fail_refresh(draw):
        raise AssertionError("verification-only lifecycle must not create predictions")

    monkeypatch.setattr(orchestrator, "refresh_next_prediction_for_draw", fail_refresh)

    def fake_import(name, *args, **kwargs):
        if name == "services.learning_engine":
            class Learning:
                @staticmethod
                def evaluate_verified_issue(issue):
                    return {"status": "ok", "issue": issue}

            return Learning
        if name == "database.analysis_store":
            class AnalysisStore:
                @staticmethod
                def save_analysis_history(draw):
                    return {"status": "ok", "issue": draw["issue"]}

            return AnalysisStore
        return real_import(name, *args, **kwargs)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)

    result = orchestrator.process_official_draw_lifecycle(
        {"issue": "100000002", "numbers": list(range(1, 21)), "super_number": 7},
        create_next_prediction=False,
        learning_synchronous=True,
    )

    assert result["prediction"]["reason"] == "create_next_prediction_disabled"


def test_process_official_draw_lifecycle_reuses_analysis_and_queues_learning(monkeypatch):
    submitted = []

    class Executor:
        def submit(self, fn, issue):
            submitted.append((fn, issue))

            class Future:
                def add_done_callback(self, callback):
                    pass

            return Future()

    monkeypatch.setattr(orchestrator, "_LEARNING_EXECUTOR", Executor())
    monkeypatch.setattr(orchestrator, "_record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "verify_prediction", lambda draw: {"status": "ok", "updated": 1})
    monkeypatch.setattr(
        orchestrator,
        "refresh_next_prediction_for_draw",
        lambda draw: {
            "status": "created",
            "analysis_record_seen": draw.get("analysis_record", {}).get("issue"),
        },
    )

    def fake_import(name, *args, **kwargs):
        if name == "database.analysis_store":
            raise AssertionError("analysis save should be reused")
        return real_import(name, *args, **kwargs)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)

    result = orchestrator.process_official_draw_lifecycle(
        {"issue": "100000002", "numbers": list(range(1, 21)), "super_number": 7},
        analysis_result={"status": "ok", "issue": "100000002", "record": {"issue": "100000002"}},
    )

    assert result["status"] == "ok"
    assert result["analysis"]["reused"] is True
    assert result["learning"]["status"] == "queued"
    assert submitted[0][1] == "100000002"
    assert result["prediction"]["analysis_record_seen"] == "100000002"


def test_background_learning_failure_does_not_fail_production(monkeypatch):
    monkeypatch.setattr(orchestrator, "_record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "verify_prediction", lambda draw: {"status": "ok", "updated": 1})
    monkeypatch.setattr(
        orchestrator,
        "_submit_learning_evaluation",
        lambda issue: {"status": "queued", "issue": issue, "async": True},
    )
    monkeypatch.setattr(
        orchestrator,
        "refresh_next_prediction_for_draw",
        lambda draw: {"status": "created", "based_on_issue": draw["issue"]},
    )

    def fake_import(name, *args, **kwargs):
        if name == "database.analysis_store":
            class AnalysisStore:
                @staticmethod
                def save_analysis_history(draw):
                    return {"status": "ok", "issue": draw["issue"]}

            return AnalysisStore
        return real_import(name, *args, **kwargs)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)

    result = orchestrator.process_official_draw_lifecycle(
        {"issue": "100000002", "numbers": list(range(1, 21)), "super_number": 7},
    )

    assert result["status"] == "ok"
    assert result["learning"]["status"] == "queued"
    assert result["prediction"]["status"] == "created"
