from collections import Counter


def analyze(draws: list[dict]) -> dict:
    if not draws:
        return {"status": "error", "message": "沒有資料"}

    latest = draws[0]
    latest_numbers = sorted(latest["numbers"])

    counter = Counter()
    for draw in draws:
        counter.update(draw["numbers"])

    hot = [n for n, _ in counter.most_common(10)]
    cold = [n for n, _ in counter.most_common()[-10:]]

    big = [n for n in latest_numbers if n >= 41]
    small = [n for n in latest_numbers if n <= 40]
    odd = [n for n in latest_numbers if n % 2 == 1]
    even = [n for n in latest_numbers if n % 2 == 0]

    consecutive = []
    for n in latest_numbers:
        if n + 1 in latest_numbers:
            consecutive.append([n, n + 1])

    brother_set = {11, 22, 33, 44, 55, 66, 77}
    brother = [n for n in latest_numbers if n in brother_set]

    diagonal = []
    for n in latest_numbers:
        if n + 9 in latest_numbers:
            diagonal.append([n, n + 9])
        if n + 11 in latest_numbers:
            diagonal.append([n, n + 11])

    repeat = []
    if len(draws) >= 2:
        repeat = sorted(set(draws[0]["numbers"]) & set(draws[1]["numbers"]))

    missing = [n for n in range(1, 81) if n not in counter]

    score_numbers = Counter()
    for n in hot:
        score_numbers[n] += 3
    for n in repeat:
        score_numbers[n] += 2
    for pair in diagonal:
        for n in pair:
            score_numbers[n] += 2
    for n in brother:
        score_numbers[n] += 1

    recommend = [n for n, _ in score_numbers.most_common(20)]

    return {
        "status": "ok",
        "title": "老玩家分析",
        "issue": latest["issue"],
        "latest_numbers": latest_numbers,
        "hot": hot,
        "cold": cold,
        "big_small": {
            "big_count": len(big),
            "small_count": len(small),
        },
        "odd_even": {
            "odd_count": len(odd),
            "even_count": len(even),
        },
        "consecutive": {
            "pairs": consecutive,
            "count": len(consecutive),
        },
        "brother": {
            "numbers": brother,
            "count": len(brother),
        },
        "diagonal": {
            "pairs": diagonal,
            "count": len(diagonal),
        },
        "repeat": repeat,
        "missing": missing[:20],
        "recommend": recommend,
    }