from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"

V6_COLUMNS = {
    "cluster_level": ("text", "text"),
    "cluster_score": ("double precision", "real"),
    "twins": ("jsonb", "text"),
    "consecutive": ("jsonb", "text"),
    "three_star": ("jsonb", "text"),
    "four_star": ("jsonb", "text"),
    "five_star": ("jsonb", "text"),
    "six_star": ("jsonb", "text"),
    "diagonal_score": ("double precision", "real"),
    "gap_score": ("double precision", "real"),
    "tail_distribution": ("jsonb", "text"),
    "hot_zone": ("jsonb", "text"),
    "cold_zone": ("jsonb", "text"),
    "patch_numbers": ("jsonb", "text"),
    "pattern": ("text", "text"),
    "ai_pattern": ("text", "text"),
}


def _now() -> str:
    return datetime.utcnow().isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_analysis_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists analysis_history (
                        issue text primary key,
                        draw_time text,
                        numbers jsonb,
                        super_number integer,
                        big_small text,
                        odd_even text,
                        consecutive_numbers jsonb,
                        repeated_numbers jsonb,
                        hot_numbers jsonb,
                        cold_numbers jsonb,
                        missing_numbers jsonb,
                        difference_values jsonb,
                        diagonal_pattern jsonb,
                        laowanjia_score jsonb,
                        ai_score jsonb,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
                for column, (cloud_type, _) in V6_COLUMNS.items():
                    cur.execute(
                        f"alter table analysis_history add column if not exists {column} {cloud_type}"
                    )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud analysis_history table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists analysis_history (
                    issue text primary key,
                    draw_time text,
                    numbers text,
                    super_number integer,
                    big_small text,
                    odd_even text,
                    consecutive_numbers text,
                    repeated_numbers text,
                    hot_numbers text,
                    cold_numbers text,
                    missing_numbers text,
                    difference_values text,
                    diagonal_pattern text,
                    laowanjia_score text,
                    ai_score text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
            existing = {
                row[1]
                for row in conn.execute("pragma table_info(analysis_history)").fetchall()
            }
            for column, (_, sqlite_type) in V6_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"alter table analysis_history add column {column} {sqlite_type}")
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite analysis_history table")

    return results


def _as_int_list(values: Any) -> list[int]:
    result = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80:
            result.append(number)
    return result


def _production_where(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}issue is not null and {prefix}issue not like '99%' and upper({prefix}issue) not like 'TEST%'"


def _recent_draws(limit: int = 120) -> list[dict]:
    try:
        from database.collector_store import get_draw_history

        return get_draw_history(limit)
    except Exception:
        logger.exception("failed to load recent draw history for analysis")
        return []


def build_analysis_record(draw: dict, recent_draws: list[dict] | None = None) -> dict:
    numbers = sorted(_as_int_list(draw.get("numbers")))
    recent = recent_draws if recent_draws is not None else _recent_draws()
    previous_numbers = _as_int_list(recent[0].get("numbers")) if recent else []

    all_numbers = []
    for item in recent:
        all_numbers.extend(_as_int_list(item.get("numbers")))
    if not all_numbers:
        all_numbers = numbers

    counter = Counter(all_numbers)
    hot_numbers = [number for number, _ in counter.most_common(10)]
    cold_numbers = [number for number, _ in counter.most_common()[-10:]]

    consecutive_numbers = []
    for number in numbers:
        if number + 1 in numbers:
            consecutive_numbers.append([number, number + 1])

    repeated_numbers = sorted(set(numbers) & set(previous_numbers))
    missing_numbers = [number for number in range(1, 81) if number not in set(all_numbers)][:30]

    difference_values = {}
    for previous in previous_numbers:
        for diff in [1, -1, 9, -9, 10, -10, 11, -11]:
            candidate = previous + diff
            if 1 <= candidate <= 80:
                difference_values.setdefault(str(diff), []).append(candidate)
    difference_values = {
        key: sorted(set(values))
        for key, values in difference_values.items()
    }

    diagonal_pattern = []
    for number in numbers:
        if number + 9 in numbers:
            diagonal_pattern.append([number, number + 9])
        if number + 11 in numbers:
            diagonal_pattern.append([number, number + 11])

    laowanjia_score = {
        "hot": len(set(numbers) & set(hot_numbers)),
        "repeat": len(repeated_numbers),
        "diagonal": len(diagonal_pattern),
        "consecutive": len(consecutive_numbers),
    }
    ai_score = {
        "score": min(
            100,
            laowanjia_score["hot"] * 5
            + laowanjia_score["repeat"] * 8
            + laowanjia_score["diagonal"] * 6
            + laowanjia_score["consecutive"] * 4,
        )
    }
    twins = [[number, number + 2] for number in numbers if number + 2 in numbers]
    consecutive = consecutive_numbers
    runs = _runs(numbers)
    three_star = [run[:3] for run in runs if len(run) >= 3]
    four_star = [run[:4] for run in runs if len(run) >= 4]
    five_star = [run[:5] for run in runs if len(run) >= 5]
    six_star = [run[:6] for run in runs if len(run) >= 6]
    decade_counts = {
        f"{start:02d}-{start + 9:02d}": sum(1 for number in numbers if start <= number <= start + 9)
        for start in range(1, 80, 10)
    }
    max_cluster = max(decade_counts.values()) if decade_counts else 0
    cluster_level = "大型群聚" if max_cluster >= 5 else "中型群聚" if max_cluster >= 3 else "小型群聚"
    cluster_score = min(100, max_cluster * 16 + len(consecutive) * 4 + len(twins) * 3)
    tail_distribution = dict(sorted(Counter(number % 10 for number in numbers).items()))
    hot_zone = [zone for zone, count in decade_counts.items() if count >= max_cluster and count > 0]
    min_cluster = min(decade_counts.values()) if decade_counts else 0
    cold_zone = [zone for zone, count in decade_counts.items() if count <= min_cluster]
    patch_numbers = _patch_numbers(numbers)
    diagonal_score = min(100, len(diagonal_pattern) * 12)
    gap_score = min(100, sum(len(values) for values in difference_values.values()) * 2)
    big_small_value = _big_small(numbers)
    odd_even_value = _odd_even(numbers)
    pattern = _pattern(cluster_level, twins, consecutive, patch_numbers)
    laowanjia_total = min(
        100,
        cluster_score * 0.20
        + diagonal_score * 0.18
        + gap_score * 0.12
        + len(twins) * 8
        + len(consecutive) * 6
        + len(patch_numbers) * 2,
    )

    return {
        "issue": str(draw.get("issue")) if draw.get("issue") is not None else None,
        "draw_time": draw.get("draw_time") or draw.get("time_text"),
        "numbers": numbers,
        "super_number": draw.get("super_number"),
        "big_small": draw.get("big_small") or big_small_value,
        "odd_even": draw.get("odd_even") or odd_even_value,
        "consecutive_numbers": consecutive_numbers,
        "repeated_numbers": repeated_numbers,
        "hot_numbers": hot_numbers,
        "cold_numbers": cold_numbers,
        "missing_numbers": missing_numbers,
        "difference_values": difference_values,
        "diagonal_pattern": diagonal_pattern,
        "laowanjia_score": round(laowanjia_total, 2),
        "laowanjia_score_detail": laowanjia_score,
        "ai_score": ai_score,
        "cluster_level": cluster_level,
        "cluster_score": round(cluster_score, 2),
        "twins": twins,
        "consecutive": consecutive,
        "three_star": three_star,
        "four_star": four_star,
        "five_star": five_star,
        "six_star": six_star,
        "diagonal_score": round(diagonal_score, 2),
        "gap_score": round(gap_score, 2),
        "tail_distribution": tail_distribution,
        "hot_zone": hot_zone,
        "cold_zone": cold_zone,
        "patch_numbers": patch_numbers,
        "pattern": pattern,
        "ai_pattern": pattern,
    }


def _runs(numbers: list[int]) -> list[list[int]]:
    number_set = set(numbers)
    runs = []
    for number in numbers:
        if number - 1 in number_set:
            continue
        run = [number]
        current = number
        while current + 1 in number_set:
            current += 1
            run.append(current)
        if len(run) >= 2:
            runs.append(run)
    return runs


def _patch_numbers(numbers: list[int]) -> list[int]:
    result = []
    for number in numbers:
        for gap in (1, 2, 9, 10, 11):
            for candidate in (number - gap, number + gap):
                if 1 <= candidate <= 80 and candidate not in numbers and candidate not in result:
                    result.append(candidate)
    return result[:12]


def _big_small(numbers: list[int]) -> str:
    big = sum(1 for number in numbers if number >= 41)
    small = len(numbers) - big
    if big > small:
        return "偏大"
    if small > big:
        return "偏小"
    return "均衡"


def _odd_even(numbers: list[int]) -> str:
    odd = sum(1 for number in numbers if number % 2)
    even = len(numbers) - odd
    if odd > even:
        return "偏單"
    if even > odd:
        return "偏雙"
    return "均衡"


def _pattern(cluster_level: str, twins: list, consecutive: list, patch_numbers: list) -> str:
    patterns = [cluster_level]
    if patch_numbers:
        patterns.append("補號模式")
    patterns.append("冷熱交替")
    if twins:
        patterns.append("雙生模式")
    if consecutive:
        patterns.append("連號模式")
    return " / ".join(patterns)


def _save_cloud(record: dict) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into analysis_history
                (
                    issue, draw_time, numbers, super_number, big_small, odd_even,
                    consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
                    missing_numbers, difference_values, diagonal_pattern,
                    laowanjia_score, ai_score,
                    cluster_level, cluster_score, twins, consecutive, three_star,
                    four_star, five_star, six_star, diagonal_score, gap_score,
                    tail_distribution, hot_zone, cold_zone, patch_numbers,
                    pattern, ai_pattern, updated_at
                )
                values (
                    %s, %s, %s::jsonb, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb,
                    %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s, now()
                )
                on conflict (issue) do update set
                    draw_time = excluded.draw_time,
                    numbers = excluded.numbers,
                    super_number = excluded.super_number,
                    big_small = excluded.big_small,
                    odd_even = excluded.odd_even,
                    consecutive_numbers = excluded.consecutive_numbers,
                    repeated_numbers = excluded.repeated_numbers,
                    hot_numbers = excluded.hot_numbers,
                    cold_numbers = excluded.cold_numbers,
                    missing_numbers = excluded.missing_numbers,
                    difference_values = excluded.difference_values,
                    diagonal_pattern = excluded.diagonal_pattern,
                    laowanjia_score = excluded.laowanjia_score,
                    ai_score = excluded.ai_score,
                    cluster_level = excluded.cluster_level,
                    cluster_score = excluded.cluster_score,
                    twins = excluded.twins,
                    consecutive = excluded.consecutive,
                    three_star = excluded.three_star,
                    four_star = excluded.four_star,
                    five_star = excluded.five_star,
                    six_star = excluded.six_star,
                    diagonal_score = excluded.diagonal_score,
                    gap_score = excluded.gap_score,
                    tail_distribution = excluded.tail_distribution,
                    hot_zone = excluded.hot_zone,
                    cold_zone = excluded.cold_zone,
                    patch_numbers = excluded.patch_numbers,
                    pattern = excluded.pattern,
                    ai_pattern = excluded.ai_pattern,
                    updated_at = now()
                """,
                _record_params(record),
            )
        conn.commit()


def _save_sqlite(record: dict) -> None:
    with _sqlite_connection() as conn:
        conn.execute(
            """
            insert into analysis_history
            (
                issue, draw_time, numbers, super_number, big_small, odd_even,
                consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
                missing_numbers, difference_values, diagonal_pattern,
                laowanjia_score, ai_score,
                cluster_level, cluster_score, twins, consecutive, three_star,
                four_star, five_star, six_star, diagonal_score, gap_score,
                tail_distribution, hot_zone, cold_zone, patch_numbers,
                pattern, ai_pattern, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(issue) do update set
                draw_time = excluded.draw_time,
                numbers = excluded.numbers,
                super_number = excluded.super_number,
                big_small = excluded.big_small,
                odd_even = excluded.odd_even,
                consecutive_numbers = excluded.consecutive_numbers,
                repeated_numbers = excluded.repeated_numbers,
                hot_numbers = excluded.hot_numbers,
                cold_numbers = excluded.cold_numbers,
                missing_numbers = excluded.missing_numbers,
                difference_values = excluded.difference_values,
                diagonal_pattern = excluded.diagonal_pattern,
                laowanjia_score = excluded.laowanjia_score,
                ai_score = excluded.ai_score,
                cluster_level = excluded.cluster_level,
                cluster_score = excluded.cluster_score,
                twins = excluded.twins,
                consecutive = excluded.consecutive,
                three_star = excluded.three_star,
                four_star = excluded.four_star,
                five_star = excluded.five_star,
                six_star = excluded.six_star,
                diagonal_score = excluded.diagonal_score,
                gap_score = excluded.gap_score,
                tail_distribution = excluded.tail_distribution,
                hot_zone = excluded.hot_zone,
                cold_zone = excluded.cold_zone,
                patch_numbers = excluded.patch_numbers,
                pattern = excluded.pattern,
                ai_pattern = excluded.ai_pattern,
                updated_at = excluded.updated_at
            """,
            _record_params(record, include_updated_at=True),
        )


def _record_params(record: dict, include_updated_at: bool = False) -> tuple:
    params = (
        record["issue"],
        record.get("draw_time"),
        _json_dumps(record.get("numbers", [])),
        record.get("super_number"),
        record.get("big_small"),
        record.get("odd_even"),
        _json_dumps(record.get("consecutive_numbers", [])),
        _json_dumps(record.get("repeated_numbers", [])),
        _json_dumps(record.get("hot_numbers", [])),
        _json_dumps(record.get("cold_numbers", [])),
        _json_dumps(record.get("missing_numbers", [])),
        _json_dumps(record.get("difference_values", {})),
        _json_dumps(record.get("diagonal_pattern", [])),
        _json_dumps(record.get("laowanjia_score", {})),
        _json_dumps(record.get("ai_score", {})),
        record.get("cluster_level"),
        record.get("cluster_score"),
        _json_dumps(record.get("twins", [])),
        _json_dumps(record.get("consecutive", [])),
        _json_dumps(record.get("three_star", [])),
        _json_dumps(record.get("four_star", [])),
        _json_dumps(record.get("five_star", [])),
        _json_dumps(record.get("six_star", [])),
        record.get("diagonal_score"),
        record.get("gap_score"),
        _json_dumps(record.get("tail_distribution", {})),
        _json_dumps(record.get("hot_zone", [])),
        _json_dumps(record.get("cold_zone", [])),
        _json_dumps(record.get("patch_numbers", [])),
        record.get("pattern"),
        record.get("ai_pattern"),
    )
    if include_updated_at:
        return (*params, _now())
    return params


def save_analysis_history(draw: dict) -> dict:
    if not draw.get("issue"):
        return {"status": "error", "storage": None, "error": "missing issue"}

    record = build_analysis_record(draw)
    if not record.get("numbers"):
        return {"status": "error", "storage": None, "issue": record.get("issue"), "error": "missing numbers"}

    try:
        _save_cloud(record)
        return {"status": "ok", "storage": "cloud", "issue": record.get("issue")}
    except Exception as exc:
        logger.exception("cloud analysis_history upsert failed")
        cloud_error = str(exc)

    try:
        _save_sqlite(record)
        return {
            "status": "ok",
            "storage": "sqlite",
            "issue": record.get("issue"),
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite analysis_history upsert failed")
        return {"status": "error", "storage": None, "issue": record.get("issue"), "error": str(exc)}


def _row_to_record(row: Any) -> dict:
    legacy_laowanjia = _json_loads(row[13])
    laowanjia_value = legacy_laowanjia
    if isinstance(legacy_laowanjia, dict):
        laowanjia_value = legacy_laowanjia.get("score")
        if laowanjia_value is None:
            laowanjia_value = min(
                100,
                (legacy_laowanjia.get("hot") or 0) * 5
                + (legacy_laowanjia.get("repeat") or 0) * 8
                + (legacy_laowanjia.get("diagonal") or 0) * 6
                + (legacy_laowanjia.get("consecutive") or 0) * 4,
            )
    return {
        "issue": row[0],
        "draw_time": row[1],
        "numbers": _json_loads(row[2]) or [],
        "super_number": row[3],
        "big_small": row[4],
        "odd_even": row[5],
        "consecutive_numbers": _json_loads(row[6]) or [],
        "repeated_numbers": _json_loads(row[7]) or [],
        "hot_numbers": _json_loads(row[8]) or [],
        "cold_numbers": _json_loads(row[9]) or [],
        "missing_numbers": _json_loads(row[10]) or [],
        "difference_values": _json_loads(row[11]) or {},
        "diagonal_pattern": _json_loads(row[12]) or [],
        "laowanjia_score": laowanjia_value if laowanjia_value is not None else legacy_laowanjia,
        "laowanjia_score_detail": legacy_laowanjia if isinstance(legacy_laowanjia, dict) else {},
        "ai_score": _json_loads(row[14]) or {},
        "created_at": str(row[15]) if row[15] is not None else None,
        "updated_at": str(row[16]) if row[16] is not None else None,
        "cluster_level": row[17] if len(row) > 17 else None,
        "cluster_score": row[18] if len(row) > 18 else None,
        "twins": _json_loads(row[19]) if len(row) > 19 else [],
        "consecutive": _json_loads(row[20]) if len(row) > 20 else [],
        "three_star": _json_loads(row[21]) if len(row) > 21 else [],
        "four_star": _json_loads(row[22]) if len(row) > 22 else [],
        "five_star": _json_loads(row[23]) if len(row) > 23 else [],
        "six_star": _json_loads(row[24]) if len(row) > 24 else [],
        "diagonal_score": row[25] if len(row) > 25 else None,
        "gap_score": row[26] if len(row) > 26 else None,
        "tail_distribution": _json_loads(row[27]) if len(row) > 27 else {},
        "hot_zone": _json_loads(row[28]) if len(row) > 28 else [],
        "cold_zone": _json_loads(row[29]) if len(row) > 29 else [],
        "patch_numbers": _json_loads(row[30]) if len(row) > 30 else [],
        "pattern": row[32] if len(row) > 32 else None,
        "ai_pattern": row[33] if len(row) > 33 else None,
    }


def _query_cloud(sql: str, params: tuple = ()) -> list[Any]:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _query_sqlite(sql: str, params: tuple = ()) -> list[Any]:
    with _sqlite_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _query_with_fallback(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    try:
        return _query_cloud(sql, params)
    except Exception:
        logger.exception("cloud analysis_history query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite analysis_history query failed")
        return []


def get_latest_analysis_history() -> dict | None:
    rows = _query_with_fallback(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit 1
        """,
    )
    return _row_to_record(rows[0]) if rows else None


def get_analysis_history(limit: int = 100) -> list[dict]:
    rows = _query_with_fallback(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at,
               cluster_level, cluster_score, twins, consecutive, three_star,
               four_star, five_star, six_star, diagonal_score, gap_score,
               tail_distribution, hot_zone, cold_zone, patch_numbers,
               laowanjia_score, pattern, ai_pattern
        from analysis_history
        where issue is not null and issue not like '99%%' and upper(issue) not like 'TEST%%'
          and cluster_level is not null
        order by issue desc
        limit ?
        """,
    )
    return [_row_to_record(row) for row in rows]


def get_analysis_statistics(limit: int = 100) -> dict:
    records = get_analysis_history(limit)
    if not records:
        return {
            "status": "empty",
            "analysis_count": 0,
            "latest_issue": None,
            "last_analysis_time": None,
            "average_laowanjia_score": 0,
            "cluster_distribution": {},
        }
    clusters = Counter(item.get("cluster_level") or "未知" for item in records)
    scores = []
    for item in records:
        try:
            scores.append(float(item.get("laowanjia_score") or 0))
        except Exception:
            pass
    return {
        "status": "ok",
        "analysis_count": len(records),
        "latest_issue": records[0].get("issue"),
        "last_analysis_time": records[0].get("updated_at") or records[0].get("created_at"),
        "average_laowanjia_score": round(sum(scores) / len(scores), 2) if scores else 0,
        "cluster_distribution": dict(clusters),
    }
