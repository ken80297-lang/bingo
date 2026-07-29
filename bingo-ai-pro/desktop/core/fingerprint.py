from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from desktop.core.backend_path import backend_root


MAIN_TABLES = [
    "official_draw_history",
    "prediction_history",
    "analysis_history",
    "rule_snapshots",
    "learning_history",
    "recommendation_runs",
    "recommendation_results",
]


def database_path() -> Path:
    return backend_root() / "data" / "bingo.db"


def sqlite_readonly_connection(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or database_path()
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)


def capture_database_fingerprint(path: Path | None = None) -> dict[str, Any]:
    db_path = path or database_path()
    if not db_path.exists():
        return {"status": "missing", "path": str(db_path), "tables": {}, "schema_hash": None}
    with sqlite_readonly_connection(db_path) as conn:
        table_rows = conn.execute("select name, sql from sqlite_master where type='table' order by name").fetchall()
        tables = {name: sql or "" for name, sql in table_rows}
        counts = {}
        for table in MAIN_TABLES:
            if table in tables:
                counts[table] = int(conn.execute(f"select count(*) from {table}").fetchone()[0] or 0)
        latest_issue = _scalar(conn, "select max(cast(issue as integer)) from official_draw_history where issue not like '99%' and upper(issue) not like 'TEST%'")
        latest_prediction = _scalar(conn, "select max(cast(prediction_issue as integer)) from prediction_history where prediction_issue not like '99%' and upper(prediction_issue) not like 'TEST%'")
        schema_payload = json.dumps(tables, sort_keys=True, ensure_ascii=False)
        return {
            "status": "ok",
            "path": str(db_path),
            "counts": counts,
            "latest_issue": str(latest_issue) if latest_issue is not None else None,
            "latest_prediction_issue": str(latest_prediction) if latest_prediction is not None else None,
            "schema_tables": sorted(tables),
            "schema_hash": hashlib.sha256(schema_payload.encode("utf-8")).hexdigest(),
        }


def fingerprints_match(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = ("counts", "latest_issue", "latest_prediction_issue", "schema_tables", "schema_hash")
    return all(before.get(key) == after.get(key) for key in keys)


def _scalar(conn: sqlite3.Connection, sql: str) -> Any:
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None

