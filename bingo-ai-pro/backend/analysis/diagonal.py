def analyze(draws: list[dict]) -> dict:
    if not draws:
        return {
            "title": "斜線",
            "description": "沒有資料",
            "numbers": []
        }

    latest = sorted(draws[0]["numbers"])

    diagonals = []

    for n in latest:
        if n + 11 in latest:
            diagonals.append([n, n + 11])

        if n + 9 in latest:
            diagonals.append([n, n + 9])

    return {
        "title": "斜線",
        "description": "本期形成的斜線",
        "numbers": diagonals,
        "count": len(diagonals)
    }