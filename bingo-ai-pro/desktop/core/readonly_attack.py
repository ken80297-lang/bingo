from __future__ import annotations

import sqlite3
from typing import Any

from desktop.core.fingerprint import sqlite_readonly_connection
from desktop.core.readonly_guard import install_readonly_guard


WRITE_ATTACKS = {
    "INSERT": "insert into official_draw_history(issue) values ('998877665544')",
    "UPDATE": "update official_draw_history set issue = issue where id = (select id from official_draw_history limit 1)",
    "DELETE": "delete from official_draw_history where id = (select id from official_draw_history limit 1)",
    "UPSERT": "insert into official_draw_history(issue) values ('999999') on conflict(issue) do update set issue = excluded.issue",
    "CREATE TABLE": "create table desktop_readonly_attack_probe(id integer)",
    "ALTER TABLE": "alter table official_draw_history add column desktop_attack_probe text",
    "DROP TABLE": "drop table official_draw_history",
}


def run_readonly_attack_suite() -> dict[str, Any]:
    guard = install_readonly_guard()
    results: dict[str, Any] = {"sql": {}, "services": {}, "select_ok": False}
    with sqlite_readonly_connection() as conn:
        conn.execute("select 1").fetchone()
        results["select_ok"] = True
        for name, sql in WRITE_ATTACKS.items():
            try:
                conn.execute(sql)
                results["sql"][name] = {"blocked": False, "error": None}
            except sqlite3.Error as exc:
                results["sql"][name] = {"blocked": True, "error": str(exc)}
    for operation in [
        "collector start",
        "catch-up apply",
        "learning run",
        "prediction publish",
        "recommendation write",
    ]:
        response = guard.block_write(operation)
        results["services"][operation] = {
            "blocked": response.get("status") == "blocked",
            "reason": response.get("reason"),
        }
    results["all_blocked"] = all(item["blocked"] for item in results["sql"].values()) and all(
        item["blocked"] for item in results["services"].values()
    )
    return results
