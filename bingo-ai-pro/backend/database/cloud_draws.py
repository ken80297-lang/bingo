import json

from database import get_connection


def insert_cloud_draw(
    issue: str,
    time_text: str,
    numbers: list[int],
    super_number: int | None = None,
    source: str = "manual",
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into draws
                (issue, draw_time, numbers, super_number, source)
                values (%s, null, %s::jsonb, %s, %s)
                on conflict (issue) do nothing
                """,
                (
                    issue,
                    json.dumps(numbers),
                    super_number,
                    source,
                ),
            )
        conn.commit()


def get_cloud_history_draws(limit: int = 80) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select issue, numbers, super_number
                from draws
                order by issue desc
                limit %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    return [
        {
            "issue": row[0],
            "time_text": None,
            "numbers": row[1],
            "super_number": row[2],
            "big_small": None,
            "odd_even": None,
        }
        for row in rows
    ]