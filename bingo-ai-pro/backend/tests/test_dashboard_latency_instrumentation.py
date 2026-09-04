from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import sys
import time
from concurrent.futures import Future

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import collector_store, learning_store, official_draw_store, prediction_history_store
from services import player_dashboard


@pytest.fixture(autouse=True)
def reset_player_dashboard_state():
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
    for key, value in list(player_dashboard._PLAYER_COMPONENT_CACHE.items()):
        player_dashboard._PLAYER_COMPONENT_CACHE[key] = [] if isinstance(value, list) else None
    player_dashboard._PLAYER_COMPONENT_CACHE["prediction_aggregates"] = {}
    player_dashboard._PLAYER_COMPONENT_CACHE["analysis"] = {}
    player_dashboard._PLAYER_COMPONENT_CACHE["kuaishou"] = {}
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT.clear()
    for key in player_dashboard._PLAYER_RUNTIME_METRICS:
        player_dashboard._PLAYER_RUNTIME_METRICS[key] = 0
    yield
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT.clear()


class FakeCursor:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.execute_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=(), **kwargs):
        self.execute_count += 1
        if self.error:
            raise self.error

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self.cursor_obj = cursor
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def cursor(self):
        return self.cursor_obj


def _messages(caplog, logger_name: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == logger_name]


def _prediction_summary_row(index: int = 0, *, issue: str | None = None, strategy: str = "production") -> tuple:
    source_issue = issue or str(115040900 + index)
    target_issue = str(int(source_issue) + 1)
    return (
        index + 1,
        source_issue,
        target_issue,
        "2026-07-30T12:00:00+08:00",
        strategy,
        0.75,
        json.dumps([1, 2, 3, 4, 5]),
        6,
        json.dumps([1, 2, 3]),
        json.dumps([1, 2, 3, 4]),
        json.dumps([]),
        json.dumps([]),
        json.dumps([]),
        json.dumps([]),
        "small",
        "odd",
        json.dumps([1, 2, 3]),
        3,
        False,
        False,
        False,
        0.6,
        "created",
        "updated",
        "model",
        "verified",
        target_issue,
        "verified",
        json.dumps([1, 2, 3]),
        json.dumps([4, 5]),
        5,
        0.6,
        False,
        False,
        0.8,
        2,
        True,
        "test-release",
    )


def _operation_event_message(issue: str, target: str, *, source: str, trigger: str, recommended_count: int = 5) -> str:
    return json.dumps(
        {
            "source": source,
            "trigger": trigger,
            "event_type": "prediction_created",
            "based_on_issue": issue,
            "target_issue": target,
            "recommended_count": recommended_count,
        }
    )


def _operation_event_row(
    issue: str,
    target: str,
    *,
    source: str,
    trigger: str,
    recommended_count: int = 5,
) -> tuple:
    return (
        issue,
        _operation_event_message(
            issue,
            target,
            source=source,
            trigger=trigger,
            recommended_count=recommended_count,
        ),
    )


def _indexed_operation_event_row(
    index: int,
    issue: str,
    target: str,
    *,
    source: str,
    trigger: str,
    recommended_count: int = 5,
) -> tuple:
    return (
        index,
        _operation_event_message(
            issue,
            target,
            source=source,
            trigger=trigger,
            recommended_count=recommended_count,
        ),
    )


def test_official_draw_latest_summary_logs_postgres_latency_once(monkeypatch, caplog):
    cursor = FakeCursor(
        rows=[
            (
                1,
                "115040900",
                "2026-07-30",
                "2026-07-30T12:00:00+08:00",
                "[1, 2, 3]",
                "[1, 2, 3]",
                3,
                False,
                "official",
                "verified",
                None,
                True,
                "created",
                "updated",
            )
        ]
    )
    conn = FakeConnection(cursor)

    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setattr(official_draw_store, "_dashboard_read_connection", lambda: conn)
    monkeypatch.setattr(official_draw_store, "_query_sqlite", lambda *args, **kwargs: pytest.fail("sqlite fallback should not run"))

    with caplog.at_level(logging.INFO, logger="database.official_draw_store"):
        result = official_draw_store.get_latest_official_draw_summary()

    assert result["issue"] == "115040900"
    assert cursor.execute_count == 1
    assert conn.closed is True
    joined = "\n".join(_messages(caplog, "database.official_draw_store"))
    assert "postgres_latency operation=official_draw_latest" in joined
    assert "connect_ms=" in joined
    assert "query_ms=" in joined
    assert "result=success" in joined
    assert "postgres://secret" not in joined


def test_kuaishou_latest_summary_logs_postgres_latency_once(monkeypatch, caplog):
    cursor = FakeCursor(rows=[(1, "115040900", "2026-07-30T12:00:00+08:00", "kuaishou", "created", "updated")])
    conn = FakeConnection(cursor)

    monkeypatch.setattr(collector_store, "_dashboard_read_connection", lambda: conn)
    monkeypatch.setattr(collector_store, "_query_sqlite", lambda *args, **kwargs: pytest.fail("sqlite fallback should not run"))

    with caplog.at_level(logging.INFO, logger="database.collector_store"):
        result = collector_store.get_latest_kuaishou_summary()

    assert result["issue"] == "115040900"
    assert cursor.execute_count == 1
    assert conn.closed is True
    joined = "\n".join(_messages(caplog, "database.collector_store"))
    assert "postgres_latency operation=kuaishou_latest" in joined
    assert "connect_ms=" in joined
    assert "query_ms=" in joined
    assert "result=success" in joined


def test_postgres_latency_connection_failure_hides_raw_error(monkeypatch, caplog):
    def fail_connect():
        raise ConnectionError("secret host and credential details")

    monkeypatch.setattr(collector_store, "_dashboard_read_connection", fail_connect)
    monkeypatch.setattr(collector_store, "_query_sqlite", lambda sql, params=(): [])

    with caplog.at_level(logging.INFO, logger="database.collector_store"):
        assert collector_store.get_latest_kuaishou_summary() is None

    joined = "\n".join(_messages(caplog, "database.collector_store"))
    assert "postgres_latency operation=kuaishou_latest" in joined
    assert "result=failed" in joined
    assert "error_type=ConnectionError" in joined
    assert "secret host" not in joined
    assert "credential" not in joined


def test_dashboard_component_latency_preserves_result(monkeypatch, caplog):
    monkeypatch.setattr(player_dashboard._PLAYER_EXECUTOR, "submit", lambda fn: _completed_future(fn()))

    with caplog.at_level(logging.INFO, logger="services.player_dashboard"):
        future, state = player_dashboard._submit_component("kuaishou", lambda: {"status": "ok"})

    assert state == "submitted"
    assert future.result() == {"status": "ok"}
    joined = "\n".join(_messages(caplog, "services.player_dashboard"))
    assert "dashboard_component_latency component=kuaishou" in joined
    assert "queue_ms=" in joined
    assert "execution_ms=" in joined
    assert "result=success" in joined


def test_dashboard_component_timeout_fallback_behavior_unchanged():
    warnings: list[str] = []
    timings: list[dict] = []
    blocked = Future()

    result = player_dashboard._component_result(
        "kuaishou",
        blocked,
        deadline=time.monotonic() + 1,
        timeout_seconds=0.001,
        timings=timings,
        warnings=warnings,
        fallback={},
    )

    assert result == {}
    assert warnings == ["kuaishou fallback cache"]
    assert timings[0]["result"] == "timeout"
    assert blocked.cancelled() is False


def test_dashboard_component_stage_latency_preserves_result(caplog):
    with caplog.at_level(logging.WARNING, logger="services.player_dashboard"):
        result = player_dashboard._timed_component_stage("card_two", "analysis_lookup", lambda: {"status": "ok"})

    assert result == {"status": "ok"}
    joined = "\n".join(_messages(caplog, "services.player_dashboard"))
    assert "component_stage_latency component=card_two stage=analysis_lookup" in joined
    assert "duration_ms=" in joined
    assert "result=success" in joined


def test_dashboard_component_stage_latency_preserves_exception(caplog):
    with caplog.at_level(logging.WARNING, logger="services.player_dashboard"):
        with pytest.raises(RuntimeError):
            player_dashboard._timed_component_stage(
                "previous_verification",
                "prediction_target_lookup",
                lambda: (_ for _ in ()).throw(RuntimeError("raw details")),
            )

    joined = "\n".join(_messages(caplog, "services.player_dashboard"))
    assert "component_stage_latency component=previous_verification stage=prediction_target_lookup" in joined
    assert "result=failed" in joined
    assert "error_type=RuntimeError" in joined
    assert "raw details" not in joined


def test_prediction_history_summary_stage_adds_no_extra_query(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_prediction_event_metadata_bulk", lambda records: ({}, 0))

    def fake_query(sql, params=(), sqlite_sql=None):
        calls.append((sql, params, sqlite_sql))
        return []

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)

    with caplog.at_level(logging.WARNING, logger="database.prediction_history_store"):
        result = prediction_history_store.get_prediction_history_summary_records(
            100,
            diagnostic_component="card_two_history",
        )

    assert result == []
    assert len(calls) == 1
    joined = "\n".join(_messages(caplog, "database.prediction_history_store"))
    assert "component_stage_latency component=card_two_history stage=prediction_history_summary_query" in joined
    assert "result=success" in joined


def test_prediction_history_summary_uses_one_cloud_metadata_bulk_query(monkeypatch, caplog):
    main_rows = [_prediction_summary_row(index) for index in range(100)]
    metadata_calls = []

    prediction_history_store._CARD_TWO_HISTORY_TIMINGS.clear()
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: True)
    monkeypatch.setattr(
        prediction_history_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: main_rows,
    )

    def fake_cloud(sql, params=()):
        metadata_calls.append((sql, params))
        return [
            _indexed_operation_event_row(
                0,
                "115040900",
                "115040901",
                source="cloud_source",
                trigger="cloud_trigger",
            )
        ]

    monkeypatch.setattr(prediction_history_store, "_query_cloud", fake_cloud)
    monkeypatch.setattr(
        prediction_history_store,
        "_query_sqlite",
        lambda *args, **kwargs: pytest.fail("sqlite metadata fallback should not run"),
    )

    with caplog.at_level(logging.WARNING, logger="database.prediction_history_store"):
        result = prediction_history_store.get_prediction_history_summary_records(
            100,
            diagnostic_component="card_two_history",
        )

    assert len(result) == 100
    assert result[0]["source"] == "cloud_source"
    assert result[0]["trigger"] == "cloud_trigger"
    assert result[1]["source"] == "production_history"
    assert len(metadata_calls) == 1
    joined = "\n".join(_messages(caplog, "database.prediction_history_store"))
    assert "card_two_history_stage_latency stage=main_query" in joined
    assert "card_two_history_stage_latency stage=transform" in joined
    assert "card_two_history_stage_latency stage=metadata_bulk" in joined
    assert "card_two_history_summary_latency" in joined
    assert "rows=100" in joined
    assert "metadata_queries=1" in joined
    status = prediction_history_store.get_card_two_history_timing_status()
    assert status["latest"]["type"] == "summary"
    assert status["latest"]["rows"] == 100
    assert status["latest"]["metadata_queries"] == 1
    assert {item.get("stage") for item in status["recent"] if item.get("type") == "stage"} == {
        "main_query",
        "transform",
        "metadata_bulk",
    }


def test_prediction_history_summary_metadata_bulk_falls_back_once(monkeypatch):
    main_rows = [_prediction_summary_row(index) for index in range(100)]
    cloud_calls = []
    sqlite_calls = []

    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: True)
    monkeypatch.setattr(
        prediction_history_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: main_rows,
    )

    def fail_cloud(sql, params=()):
        cloud_calls.append((sql, params))
        raise ConnectionError("hidden credentials")

    def fake_sqlite(sql, params=()):
        sqlite_calls.append((sql, params))
        return [
            _indexed_operation_event_row(
                0,
                "115040900",
                "115040901",
                source="sqlite_source",
                trigger="sqlite_trigger",
            )
        ]

    monkeypatch.setattr(prediction_history_store, "_query_cloud", fail_cloud)
    monkeypatch.setattr(prediction_history_store, "_query_sqlite", fake_sqlite)

    result = prediction_history_store.get_prediction_history_summary_records(100)

    assert len(result) == 100
    assert result[0]["source"] == "sqlite_source"
    assert result[0]["trigger"] == "sqlite_trigger"
    assert len(cloud_calls) == 1
    assert len(sqlite_calls) == 1


def test_prediction_history_bulk_metadata_matches_single_record_precedence(monkeypatch):
    records = [
        prediction_history_store._row_to_prediction_summary(_prediction_summary_row(0)),
        prediction_history_store._row_to_prediction_summary(_prediction_summary_row(1)),
        prediction_history_store._row_to_prediction_summary(_prediction_summary_row(2)),
    ]
    rows = [
        _operation_event_row("115040900", "115040901", source="newer", trigger="newer_trigger"),
        _operation_event_row("115040900", "115040901", source="older", trigger="older_trigger"),
        _operation_event_row("other", "115040903", source="message_match", trigger="message_trigger"),
    ]

    def fake_single_query(sql, params=(), sqlite_sql=None):
        based_on, pattern = params
        target = pattern.strip("%")
        for row in rows:
            if row[0] == based_on or target in row[1]:
                return [(row[1],)]
        return []

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_single_query)
    single = [prediction_history_store._prediction_event_metadata(record) for record in records]

    indexed_rows = [
        (0, rows[0][1]),
        (1, None),
        (2, rows[2][1]),
    ]
    bulk = prediction_history_store._metadata_map_from_indexed_rows(records, indexed_rows)
    enriched = [
        prediction_history_store._enrich_prediction_metadata_from_map(record.copy(), bulk.get(id(record)))
        for record in records
    ]

    assert enriched[0]["source"] == single[0]["source"] == "newer"
    assert enriched[0]["trigger"] == single[0]["trigger"] == "newer_trigger"
    assert enriched[1]["source"] == single[1].get("source", "production_history")
    assert enriched[2]["source"] == single[2]["source"] == "message_match"
    assert enriched[2]["trigger"] == single[2]["trigger"] == "message_trigger"


def test_prediction_history_bulk_metadata_preserves_equal_priority_semantics(monkeypatch, tmp_path):
    db_path = tmp_path / "metadata_equal_priority.db"
    records = [
        {"issue": "115040901", "prediction_issue": "115040902"},
        {"issue": "115040903", "prediction_issue": "115040904"},
        {"issue": "115040905", "prediction_issue": "115040906"},
        {"issue": "115040907", "prediction_issue": "115040908"},
        {"issue": "115040909", "prediction_issue": "115040910"},
        {"issue": "115040911", "prediction_issue": "115040912"},
        {"issue": "115040913", "prediction_issue": "115040914"},
    ]

    def connect():
        return sqlite3.connect(db_path)

    with connect() as conn:
        conn.execute(
            """
            create table operation_events (
                id integer primary key,
                issue text,
                event_type text,
                message text,
                created_at text
            )
            """
        )
        events = [
            (1, "115040901", "prediction_created", _operation_event_message("115040901", "other", source="issue_only", trigger="issue"), "2026-07-30T10:00:00"),
            (2, "other", "prediction_created", _operation_event_message("other", "115040904", source="message_only", trigger="message"), "2026-07-30T10:01:00"),
            (3, "115040905", "prediction_created", _operation_event_message("115040905", "old", source="issue_older", trigger="issue"), "2026-07-30T10:02:00"),
            (4, "other", "prediction_created", _operation_event_message("other", "115040906", source="message_newer", trigger="message"), "2026-07-30T10:03:00"),
            (5, "other", "prediction_created", _operation_event_message("other", "115040908", source="message_older", trigger="message"), "2026-07-30T10:04:00"),
            (6, "115040907", "prediction_created", _operation_event_message("115040907", "old", source="issue_newer", trigger="issue"), "2026-07-30T10:05:00"),
            (7, "115040909", "prediction_created", _operation_event_message("115040909", "115040910", source="same_event", trigger="both"), "2026-07-30T10:06:00"),
            (8, "115040911", "prediction_created", _operation_event_message("115040911", "old", source="same_time_lower_id", trigger="issue"), "2026-07-30T10:07:00"),
            (9, "other", "prediction_created", _operation_event_message("other", "115040912", source="same_time_higher_id", trigger="message"), "2026-07-30T10:07:00"),
            (10, "115040901", "other_event", _operation_event_message("115040901", "115040902", source="wrong_type", trigger="ignored"), "2026-07-30T10:08:00"),
        ]
        conn.executemany(
            "insert into operation_events (id, issue, event_type, message, created_at) values (?, ?, ?, ?, ?)",
            events,
        )

    sqlite_calls = []
    original_query_sqlite = prediction_history_store._query_sqlite

    def counting_query_sqlite(sql, params=()):
        sqlite_calls.append((sql, params))
        return original_query_sqlite(sql, params)

    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: False)
    monkeypatch.setattr(prediction_history_store, "_sqlite_connection", connect)
    monkeypatch.setattr(prediction_history_store, "_query_sqlite", counting_query_sqlite)

    metadata, queries = prediction_history_store._prediction_event_metadata_bulk(records)

    assert queries == 1
    assert len(sqlite_calls) == 1
    assert "left join lateral" not in sqlite_calls[0][0].lower()
    assert "union all" in sqlite_calls[0][0].lower()
    enriched = [
        prediction_history_store._enrich_prediction_metadata_from_map(record.copy(), metadata.get(id(record)))
        for record in records
    ]
    assert [item.get("source") for item in enriched] == [
        "issue_only",
        "message_only",
        "message_newer",
        "issue_newer",
        "same_event",
        "same_time_higher_id",
        "production_history",
    ]


def test_prediction_history_bulk_metadata_cloud_uses_set_based_equal_priority_query(monkeypatch):
    records = [
        prediction_history_store._row_to_prediction_summary(_prediction_summary_row(index))
        for index in range(100)
    ]
    cloud_calls = []

    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: True)

    def fake_cloud(sql, params=()):
        cloud_calls.append((sql, params))
        return [_indexed_operation_event_row(0, "115040900", "115040901", source="cloud_source", trigger="cloud")]

    monkeypatch.setattr(prediction_history_store, "_query_cloud", fake_cloud)
    monkeypatch.setattr(
        prediction_history_store,
        "_query_sqlite",
        lambda *args, **kwargs: pytest.fail("sqlite metadata fallback should not run"),
    )

    metadata, queries = prediction_history_store._prediction_event_metadata_bulk(records)

    assert queries == 1
    assert metadata[id(records[0])]["source"] == "cloud_source"
    assert len(cloud_calls) == 1
    sql, params = cloud_calls[0]
    lowered = sql.lower()
    assert "left join lateral" not in lowered
    assert "union all" in lowered
    assert "row_number() over" in lowered
    assert "order by created_at desc, id desc" in lowered
    assert len(params) == 300


def test_card_two_history_records_db_timing_breakdown(monkeypatch):
    main_rows = [_prediction_summary_row(index) for index in range(3)]
    metadata_rows = [
        _indexed_operation_event_row(0, "115040900", "115040901", source="cloud_source", trigger="cloud"),
    ]
    cursors = [FakeCursor(rows=main_rows), FakeCursor(rows=metadata_rows)]
    connections = []

    def fake_cloud_connection():
        conn = FakeConnection(cursors[len(connections)])
        connections.append(conn)
        return conn

    prediction_history_store._CARD_TWO_HISTORY_TIMINGS.clear()
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: True)
    monkeypatch.setattr(prediction_history_store, "_cloud_connection", fake_cloud_connection)
    monkeypatch.setattr(
        prediction_history_store,
        "_query_sqlite",
        lambda *args, **kwargs: pytest.fail("sqlite fallback should not run"),
    )

    result = prediction_history_store.get_prediction_history_summary_records(
        3,
        diagnostic_component="card_two_history",
    )

    assert len(result) == 3
    assert len(connections) == 2
    status = prediction_history_store.get_card_two_history_timing_status()
    stages = {
        item["stage"]: item
        for item in status["recent"]
        if item.get("type") == "stage"
    }
    for stage, expected_rows in (("main_query", 3), ("metadata_bulk", 1)):
        db_timing = stages[stage]["db_timing"]
        assert db_timing["backend"] == "postgres"
        assert db_timing["result"] == "success"
        assert db_timing["row_count"] == expected_rows
        assert set(db_timing) >= {"connect_ms", "execute_ms", "fetch_ms", "total_ms"}
    assert status["latest"]["metadata_queries"] == 1


def test_card_two_query_timing_records_cloud_failure(monkeypatch):
    timing = {}

    def fail_cloud_connection():
        raise ConnectionError("secret host details")

    monkeypatch.setattr(prediction_history_store, "_cloud_connection", fail_cloud_connection)

    with pytest.raises(ConnectionError):
        prediction_history_store._with_card_two_query_timing(
            timing,
            lambda: prediction_history_store._query_cloud("select 1"),
        )

    assert timing["backend"] == "postgres"
    assert timing["result"] == "failed"
    assert timing["error_type"] == "ConnectionError"
    assert "total_ms" in timing
    assert "secret host" not in str(timing)


def test_prediction_history_summary_filter_order_limit_and_schema_preserved(monkeypatch):
    rows = [
        _prediction_summary_row(0),
        _prediction_summary_row(1, strategy="test"),
        _prediction_summary_row(2),
    ]
    captured = {}

    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)

    def fake_query(sql, params=(), sqlite_sql=None):
        captured["params"] = params
        return rows

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)
    monkeypatch.setattr(prediction_history_store, "_prediction_event_metadata_bulk", lambda records: ({}, 0))

    result = prediction_history_store.get_prediction_history_summary_records(100)

    assert captured["params"] == (100,)
    assert [item["issue"] for item in result] == ["115040900", "115040902"]
    assert result[0]["read_layer"]["query_name"] == "production_prediction_history_summary_v1"
    assert "recommend_numbers" in result[0]
    assert "operation_event" not in result[0]


def test_card_two_history_timing_status_is_bounded_process_memory():
    prediction_history_store._CARD_TWO_HISTORY_TIMINGS.clear()

    for index in range(prediction_history_store._CARD_TWO_HISTORY_TIMING_LIMIT + 5):
        prediction_history_store._record_card_two_history_timing(
            {
                "type": "stage",
                "stage": f"stage_{index}",
                "duration_ms": index,
                "result": "success",
            }
        )

    status = prediction_history_store.get_card_two_history_timing_status()

    assert status["limit"] == prediction_history_store._CARD_TWO_HISTORY_TIMING_LIMIT
    assert len(status["recent"]) == prediction_history_store._CARD_TWO_HISTORY_TIMING_LIMIT
    assert status["latest"]["stage"] == f"stage_{prediction_history_store._CARD_TWO_HISTORY_TIMING_LIMIT + 4}"
    status["recent"].append({"stage": "mutated"})
    assert len(prediction_history_store.get_card_two_history_timing_status()["recent"]) == (
        prediction_history_store._CARD_TWO_HISTORY_TIMING_LIMIT
    )


def test_prediction_aggregates_stage_adds_no_extra_query(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(learning_store, "get_learned_live_target_count", lambda: 7)

    def fake_query(sql, params=(), sqlite_sql=None):
        calls.append((sql, params, sqlite_sql))
        return [(10, 9, 1, 8, 6, 6, 5)]

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)

    with caplog.at_level(logging.WARNING, logger="database.prediction_history_store"):
        result = prediction_history_store.get_prediction_lifecycle_aggregates(
            diagnostic_component="prediction_aggregates",
        )

    assert result["total_prediction_count"] == 10
    assert result["learned_distinct_target_count"] == 7
    assert len(calls) == 2
    joined = "\n".join(_messages(caplog, "database.prediction_history_store"))
    assert "component_stage_latency component=prediction_aggregates stage=learned_live_target_count" in joined
    assert "component_stage_latency component=prediction_aggregates stage=prediction_history_aggregate_query" in joined
    assert "component_stage_latency component=prediction_aggregates stage=official_result_join_count" in joined


def test_prediction_history_stage_logging_is_dashboard_opt_in(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)

    def fake_query(sql, params=(), sqlite_sql=None):
        calls.append((sql, params, sqlite_sql))
        return []

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)

    with caplog.at_level(logging.WARNING, logger="database.prediction_history_store"):
        result = prediction_history_store.get_prediction_history_summary_records(100)

    assert result == []
    assert len(calls) == 1
    joined = "\n".join(_messages(caplog, "database.prediction_history_store"))
    assert "component_stage_latency" not in joined


def _completed_future(value):
    future = Future()
    future.set_result(value)
    return future
