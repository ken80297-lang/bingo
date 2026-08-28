from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import rule_snapshot_store
from services.rule_snapshot import build_rule_snapshot


def _use_temp_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_TYPE", raising=False)
    monkeypatch.setattr(rule_snapshot_store, "SQLITE_PATH", tmp_path / "rule_snapshots.db")


def _snapshot(version: str = "test") -> dict:
    return build_rule_snapshot(
        {"issue": "115000100", "hot_numbers": [1, 2, 3]},
        {"prediction_issue": "115000101", "recommend_numbers": [1, 2, 3]},
        generated_at="2026-07-22T00:00:00+00:00",
        rule_library_version=version,
    )


def test_rule_snapshot_store_sqlite_roundtrip(monkeypatch, tmp_path):
    _use_temp_sqlite(monkeypatch, tmp_path)

    assert rule_snapshot_store.init_rule_snapshot_tables()["sqlite"] == "available"
    saved = rule_snapshot_store.save_rule_snapshot(_snapshot())
    loaded = rule_snapshot_store.get_rule_snapshot(
        source_issue="115000100",
        target_issue="115000101",
        rule_library_version="test",
    )

    assert saved["status"] == "ok"
    assert saved["storage"] == "sqlite"
    assert loaded is not None
    assert loaded["source_issue"] == "115000100"
    assert loaded["target_issue"] == "115000101"
    assert loaded["snapshot_json"]["aggregate"]["total_count"] > 0


def test_rule_snapshot_store_sqlite_upserts_same_snapshot_key(monkeypatch, tmp_path):
    _use_temp_sqlite(monkeypatch, tmp_path)
    rule_snapshot_store.init_rule_snapshot_tables()

    first = _snapshot()
    second = _snapshot()
    second["rules"][0]["candidate_numbers"] = [9]

    first_saved = rule_snapshot_store.save_rule_snapshot(first)
    second_saved = rule_snapshot_store.save_rule_snapshot(second)
    latest = rule_snapshot_store.get_latest_rule_snapshot()

    assert first_saved["id"] == second_saved["id"]
    assert latest["snapshot_json"]["rules"][0]["candidate_numbers"] == [9]


def test_rule_snapshot_store_rejects_missing_required_keys(monkeypatch, tmp_path):
    _use_temp_sqlite(monkeypatch, tmp_path)

    result = rule_snapshot_store.save_rule_snapshot({"source_issue": "115000100"})

    assert result["status"] == "error"
    assert result["error"] == "missing source_issue or rule_library_version"


def test_rule_snapshot_store_upserts_missing_target_issue(monkeypatch, tmp_path):
    _use_temp_sqlite(monkeypatch, tmp_path)
    rule_snapshot_store.init_rule_snapshot_tables()

    first = build_rule_snapshot(
        {"issue": "115000100", "hot_numbers": [1]},
        generated_at="2026-07-22T00:00:00+00:00",
        rule_library_version="test",
    )
    second = build_rule_snapshot(
        {"issue": "115000100", "hot_numbers": [2]},
        generated_at="2026-07-22T00:01:00+00:00",
        rule_library_version="test",
    )

    first_saved = rule_snapshot_store.save_rule_snapshot(first)
    second_saved = rule_snapshot_store.save_rule_snapshot(second)
    loaded = rule_snapshot_store.get_rule_snapshot(source_issue="115000100", rule_library_version="test")

    assert first_saved["id"] == second_saved["id"]
    assert loaded["target_issue"] is None
    assert loaded["snapshot_json"]["rules"][0]["candidate_numbers"] == [2]
