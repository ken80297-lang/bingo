from __future__ import annotations

from services import date_shadow_learning


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = ()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params, prepare=False):
        self.sql = sql
        self.params = params
    def fetchall(self): return self.rows


class _Conn:
    def __init__(self, rows): self.rows = rows
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return _Cursor(self.rows)


def test_date_shadow_learning_uses_historical_snapshots(monkeypatch):
    rows = [
        ("115054900", list(range(1, 21)), {"ai_score": {"long_dragon": {"candidate_numbers": list(range(1, 21))}}}),
        ("115054899", list(range(1, 21)), {"ai_score": {"long_dragon": {"candidate_numbers": list(range(1, 21))}}}),
    ]
    monkeypatch.setattr(date_shadow_learning, "get_connection", lambda: _Conn(rows))
    result = date_shadow_learning.evaluate_shadow_rules_for_date("2026-09-28")
    assert result["status"] == "ok"
    assert result["official_targets_with_snapshot"] == 2
    assert result["first_issue"] == "115054899"
    assert result["last_issue"] == "115054900"
    assert result["rules"]["long_dragon"]["sample_size"] == 2
    assert result["method"] == "historical_snapshot_only"
