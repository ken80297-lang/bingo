from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import rule_snapshots as rule_snapshots_api


def test_rule_snapshot_health_endpoint_returns_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rule_snapshots_api,
        "build_rule_snapshot_health",
        lambda limit=100: calls.append(limit) or {
            "status": "ok",
            "analysis_count": 2,
            "snapshot_count": 2,
            "coverage_rate": 100,
            "warnings": [],
        },
    )

    result = rule_snapshots_api.api_rule_snapshot_health(limit=25)

    assert calls == [25]
    assert result["status"] == "ok"
    assert result["coverage_rate"] == 100


def test_rule_snapshot_health_endpoint_clamps_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rule_snapshots_api,
        "build_rule_snapshot_health",
        lambda limit=100: calls.append(limit) or {"status": "ok", "warnings": []},
    )

    high = rule_snapshots_api.api_rule_snapshot_health(limit=999)
    low = rule_snapshots_api.api_rule_snapshot_health(limit=0)

    assert calls == [500, 1]
    assert high["status"] == "ok"
    assert low["status"] == "ok"


def test_rule_snapshot_health_endpoint_returns_error_payload_on_exception(monkeypatch):
    def fail(limit=100):
        raise RuntimeError("health unavailable")

    monkeypatch.setattr(rule_snapshots_api, "build_rule_snapshot_health", fail)

    result = rule_snapshots_api.api_rule_snapshot_health(limit=20)

    assert result["status"] == "error"
    assert result["limit"] == 20
    assert result["error"] == "health unavailable"
    assert "rule_snapshot_health_failed" in result["warnings"]


def test_rule_snapshot_audit_endpoint_clamps_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rule_snapshots_api,
        "audit_rule_snapshot_fast_path",
        lambda limit=20: calls.append(limit) or {"status": "ok", "limit": limit, "items": []},
    )

    high = rule_snapshots_api.api_rule_snapshot_audit(limit=999)
    low = rule_snapshots_api.api_rule_snapshot_audit(limit=0)

    assert calls == [100, 1]
    assert high["limit"] == 100
    assert low["limit"] == 1


def test_rule_snapshot_audit_endpoint_returns_error_payload_on_exception(monkeypatch):
    def fail(limit=20):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(rule_snapshots_api, "audit_rule_snapshot_fast_path", fail)

    result = rule_snapshots_api.api_rule_snapshot_audit(limit=20)

    assert result["status"] == "error"
    assert result["reason"] == "rule_snapshot_audit_failed"
    assert result["limit"] == 20
    assert result["total_compared"] == 0
    assert result["error"] == "audit unavailable"


def test_rule_snapshot_generate_endpoint_defaults_persist_false(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rule_snapshots_api,
        "generate_rule_snapshot_for_issue",
        lambda source_issue, target_issue=None, persist=True: calls.append(
            {"source_issue": source_issue, "target_issue": target_issue, "persist": persist}
        ) or {"status": "ok", "persisted": persist},
    )

    result = rule_snapshots_api.api_rule_snapshot_generate(
        rule_snapshots_api.GenerateRuleSnapshotRequest(source_issue="115000100", target_issue="115000101")
    )

    assert calls == [{"source_issue": "115000100", "target_issue": "115000101", "persist": False}]
    assert result["status"] == "ok"
    assert result["persisted"] is False


def test_rule_snapshot_compare_endpoint_returns_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rule_snapshots_api,
        "compare_rule_snapshot_fast_path",
        lambda source_issue, target_issue=None: calls.append(
            {"source_issue": source_issue, "target_issue": target_issue}
        ) or {
            "status": "ok",
            "source_issue": source_issue,
            "target_issue": target_issue,
            "analysis_path_numbers": [1, 2],
            "snapshot_path_numbers": [2, 3],
            "overlap_count": 1,
            "overlap_numbers": [2],
            "added_by_snapshot": [3],
            "removed_by_snapshot": [1],
            "snapshot_used": True,
            "fallback_used": False,
            "warnings": [],
        },
    )

    result = rule_snapshots_api.api_rule_snapshot_compare("115000100", "115000101")

    assert calls == [{"source_issue": "115000100", "target_issue": "115000101"}]
    assert result["status"] == "ok"
    assert result["overlap_numbers"] == [2]
    assert result["snapshot_used"] is True


def test_rule_snapshot_compare_endpoint_rejects_missing_source_issue(monkeypatch):
    monkeypatch.setattr(
        rule_snapshots_api,
        "compare_rule_snapshot_fast_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("missing source should not call service")),
    )

    result = rule_snapshots_api.api_rule_snapshot_compare(source_issue="", target_issue="115000101")

    assert result["status"] == "error"
    assert result["reason"] == "missing_source_issue"
    assert result["snapshot_used"] is False


def test_rule_snapshot_compare_endpoint_returns_error_payload_on_exception(monkeypatch):
    def fail(source_issue, target_issue=None):
        raise RuntimeError("compare unavailable")

    monkeypatch.setattr(rule_snapshots_api, "compare_rule_snapshot_fast_path", fail)

    result = rule_snapshots_api.api_rule_snapshot_compare("115000100", "115000101")

    assert result["status"] == "error"
    assert result["reason"] == "rule_snapshot_compare_failed"
    assert result["source_issue"] == "115000100"
    assert result["target_issue"] == "115000101"
    assert result["error"] == "compare unavailable"


def test_rule_snapshot_generate_endpoint_passes_persist_true(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rule_snapshots_api,
        "generate_rule_snapshot_for_issue",
        lambda source_issue, target_issue=None, persist=True: calls.append(
            {"source_issue": source_issue, "target_issue": target_issue, "persist": persist}
        ) or {"status": "ok", "persisted": persist},
    )

    result = rule_snapshots_api.api_rule_snapshot_generate(
        rule_snapshots_api.GenerateRuleSnapshotRequest(
            source_issue="115000100",
            target_issue=None,
            persist=True,
        )
    )

    assert calls == [{"source_issue": "115000100", "target_issue": None, "persist": True}]
    assert result["status"] == "ok"
    assert result["persisted"] is True


def test_rule_snapshot_generate_endpoint_rejects_missing_source_issue(monkeypatch):
    monkeypatch.setattr(
        rule_snapshots_api,
        "generate_rule_snapshot_for_issue",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("missing source should not call service")),
    )

    result = rule_snapshots_api.api_rule_snapshot_generate(
        rule_snapshots_api.GenerateRuleSnapshotRequest(target_issue="115000101", persist=True)
    )

    assert result["status"] == "error"
    assert result["reason"] == "missing_source_issue"
    assert result["persisted"] is False


def test_rule_snapshot_generate_endpoint_returns_error_payload_on_exception(monkeypatch):
    def fail(source_issue, target_issue=None, persist=True):
        raise RuntimeError("generate unavailable")

    monkeypatch.setattr(rule_snapshots_api, "generate_rule_snapshot_for_issue", fail)

    result = rule_snapshots_api.api_rule_snapshot_generate(
        rule_snapshots_api.GenerateRuleSnapshotRequest(source_issue="115000100", persist=False)
    )

    assert result["status"] == "error"
    assert result["reason"] == "rule_snapshot_generate_failed"
    assert result["source_issue"] == "115000100"
    assert result["persist"] is False
    assert result["persisted"] is False
    assert result["error"] == "generate unavailable"
