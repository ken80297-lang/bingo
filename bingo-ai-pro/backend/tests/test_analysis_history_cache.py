from database import analysis_store


def _record(issue: str) -> dict:
    return {"issue": issue, "numbers": list(range(1, 21)), "cluster_level": "normal"}


def setup_function():
    analysis_store.clear_analysis_history_cache()


def teardown_function():
    analysis_store.clear_analysis_history_cache()


def test_cache_miss_loads_database_then_hit_avoids_database(monkeypatch):
    calls = []
    rows = [_record(str(200 - i)) for i in range(100)]
    monkeypatch.setattr(analysis_store, "get_analysis_history", lambda limit: calls.append(limit) or rows[:limit])

    first, first_meta = analysis_store.get_cached_analysis_history(100, based_on_issue="200")
    second, second_meta = analysis_store.get_cached_analysis_history(100, based_on_issue="200")

    assert len(first) == 100
    assert first_meta["source"] == "database"
    assert second_meta["source"] == "memory"
    assert calls == [100]
    assert second == first


def test_cache_replaces_duplicate_issue_and_caps_at_100():
    for issue in range(100, 201):
        analysis_store._update_analysis_history_cache(_record(str(issue)))
    analysis_store._update_analysis_history_cache({**_record("200"), "cluster_level": "updated"})

    rows, meta = analysis_store.get_cached_analysis_history(100, based_on_issue="200")

    assert meta["source"] == "memory"
    assert len(rows) == 100
    assert rows[0]["issue"] == "200"
    assert rows[0]["cluster_level"] == "updated"
    assert len([row for row in rows if row["issue"] == "200"]) == 1
    assert rows[-1]["issue"] == "101"


def test_stale_based_on_issue_falls_back_and_rebuilds(monkeypatch):
    for issue in range(101, 201):
        analysis_store._update_analysis_history_cache(_record(str(issue)))
    fresh = [_record(str(201 - i)) for i in range(100)]
    calls = []
    monkeypatch.setattr(analysis_store, "get_analysis_history", lambda limit: calls.append(limit) or fresh[:limit])

    rows, meta = analysis_store.get_cached_analysis_history(100, based_on_issue="201")
    again, again_meta = analysis_store.get_cached_analysis_history(100, based_on_issue="201")

    assert meta["source"] == "database"
    assert meta["cache_reason"] == "stale_based_on_issue"
    assert rows[0]["issue"] == "201"
    assert again_meta["source"] == "memory"
    assert again == rows
    assert calls == [100]


def test_incomplete_cache_falls_back_to_database(monkeypatch):
    analysis_store._update_analysis_history_cache(_record("200"))
    rows = [_record(str(200 - i)) for i in range(100)]
    calls = []
    monkeypatch.setattr(analysis_store, "get_analysis_history", lambda limit: calls.append(limit) or rows[:limit])

    result, meta = analysis_store.get_cached_analysis_history(100, based_on_issue="200")

    assert len(result) == 100
    assert meta["source"] == "database"
    assert meta["cache_reason"] == "cold_or_incomplete"
    assert calls == [100]


def test_sqlite_fallback_does_not_promote_degraded_write_into_prediction_cache(monkeypatch):
    rows = [_record(str(200 - i)) for i in range(100)]
    monkeypatch.setattr(analysis_store, "get_analysis_history", lambda limit: rows[:limit])
    analysis_store.get_cached_analysis_history(100, based_on_issue="200")

    monkeypatch.setattr(analysis_store, "build_analysis_record", lambda draw: _record("201"))
    monkeypatch.setattr(analysis_store, "_save_cloud", lambda record: (_ for _ in ()).throw(RuntimeError("cloud down")))
    monkeypatch.setattr(analysis_store, "_save_sqlite", lambda record: None)

    result = analysis_store.save_analysis_history({"issue": "201"})
    cached, meta = analysis_store.get_cached_analysis_history(100, based_on_issue="200")

    assert result["status"] == "ok"
    assert result["storage"] == "sqlite"
    assert meta["source"] == "memory"
    assert cached[0]["issue"] == "200"
    assert all(row["issue"] != "201" for row in cached)
