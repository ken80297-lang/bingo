def analyze(draws: list[dict]) -> dict:
    """
    缺號分析
    找出最近資料中沒有出現過的號碼
    """

    appeared = set()

    for draw in draws:
        appeared.update(draw["numbers"])

    missing = sorted(set(range(1, 81)) - appeared)

    return {
        "title": "缺號",
        "description": "最近資料未開出的號碼",
        "missing_numbers": [f"{n:02d}" for n in missing],
        "count": len(missing)
    }