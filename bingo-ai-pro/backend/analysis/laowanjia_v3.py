from collections import Counter, defaultdict


BROTHER_NUMBERS = {11, 22, 33, 44, 55, 66, 77}


WEIGHTS = {
    "recent_20_hot": 24,
    "recent_60_hot": 16,
    "recent_120_hot": 10,
    "repeat": 18,
    "diagonal": 14,
    "difference": 10,
    "brother": 8,
    "tail_hot": 6,
    "cold_rebound": 5,
}


def _tail(n: int) -> int:
    return n % 10


def _hot_numbers(draws: list[dict], limit: int, top: int = 15) -> list[int]:
    counter = Counter()
    for draw in draws[:limit]:
        counter.update(draw["numbers"])
    return [n for n, _ in counter.most_common(top)]


def _tail_counter(draws: list[dict], limit: int) -> Counter:
    counter = Counter()
    for draw in draws[:limit]:
        counter.update([_tail(n) for n in draw["numbers"]])
    return counter


def _diagonal_pairs(numbers: list[int]) -> list[list[int]]:
    nums = sorted(numbers)
    pairs = []
    for n in nums:
        if n + 9 in nums:
            pairs.append([n, n + 9])
        if n + 11 in nums:
            pairs.append([n, n + 11])
    return pairs


def _difference_candidates(previous_numbers: list[int]) -> dict[str, list[int]]:
    result: dict[str, set[int]] = defaultdict(set)

    for n in previous_numbers:
        for diff in [1, -1, 9, -9, 10, -10, 11, -11]:
            candidate = n + diff
            if 1 <= candidate <= 80:
                result[str(diff)].add(candidate)

    return {key: sorted(value) for key, value in result.items()}


def _missing_numbers(draws: list[dict]) -> list[int]:
    appeared = set()
    for draw in draws:
        appeared.update(draw["numbers"])
    return [n for n in range(1, 81) if n not in appeared]


def analyze_v3(draws: list[dict]) -> dict:
    if not draws:
        return {"status": "error", "message": "沒有資料"}

    latest = draws[0]
    latest_numbers = sorted(latest["numbers"])
    previous_numbers = draws[1]["numbers"] if len(draws) >= 2 else []

    hot20 = _hot_numbers(draws, 20)
    hot60 = _hot_numbers(draws, 60)
    hot120 = _hot_numbers(draws, 120)

    tail_rank = _tail_counter(draws, 120).most_common()
    hot_tails = [tail for tail, _ in tail_rank[:3]]

    diagonal = _diagonal_pairs(latest_numbers)
    difference = _difference_candidates(previous_numbers)

    repeat = sorted(set(draws[0]["numbers"]) & set(draws[1]["numbers"])) if len(draws) >= 2 else []
    brother = [n for n in latest_numbers if n in BROTHER_NUMBERS]
    missing = _missing_numbers(draws)

    score = Counter()
    score_detail: dict[int, dict[str, int]] = defaultdict(dict)
    reasons: dict[int, list[str]] = defaultdict(list)

    def add_score(number: int, key: str, points: int, reason: str) -> None:
        score[number] += points
        score_detail[number][key] = score_detail[number].get(key, 0) + points
        if reason not in reasons[number]:
            reasons[number].append(reason)

    for n in hot20:
        add_score(n, "recent_20_hot", WEIGHTS["recent_20_hot"], "近20期熱號")

    for n in hot60:
        add_score(n, "recent_60_hot", WEIGHTS["recent_60_hot"], "近60期熱號")

    for n in hot120:
        add_score(n, "recent_120_hot", WEIGHTS["recent_120_hot"], "近120期熱號")

    for n in repeat:
        add_score(n, "repeat", WEIGHTS["repeat"], "重號")

    for pair in diagonal:
        for n in pair:
            add_score(n, "diagonal", WEIGHTS["diagonal"], "斜線")

    for diff_key, nums in difference.items():
        for n in nums:
            add_score(n, "difference", WEIGHTS["difference"], f"相差{diff_key}")

    for n in brother:
        add_score(n, "brother", WEIGHTS["brother"], "雙生號")

    for n in range(1, 81):
        if _tail(n) in hot_tails:
            add_score(n, "tail_hot", WEIGHTS["tail_hot"], "熱門尾數")

    for n in missing[:20]:
        add_score(n, "cold_rebound", WEIGHTS["cold_rebound"], "缺號回補")

    ranked = []
    for n, raw_score in score.most_common():
        normalized = min(100, raw_score)
        ranked.append(
            {
                "number": n,
                "score": normalized,
                "raw_score": raw_score,
                "grade": (
                    "S" if normalized >= 85 else
                    "A" if normalized >= 70 else
                    "B" if normalized >= 55 else
                    "C"
                ),
                "reasons": reasons[n],
                "detail": score_detail[n],
            }
        )

    top20 = [item["number"] for item in ranked[:20]]
    top10 = [item["number"] for item in ranked[:10]]

    super_counter = Counter()
    for draw in draws[:120]:
        super_number = draw.get("super_number")
        if super_number:
            super_counter.update([super_number])

    super_candidates = [n for n, _ in super_counter.most_common(5)]
    if not super_candidates:
        super_candidates = top20[:5]

    return {
        "status": "ok",
        "title": "老玩家 AI Pro V3",
        "issue": latest["issue"],
        "latest_numbers": latest_numbers,
        "weights": WEIGHTS,
        "trend": {
            "hot20": hot20,
            "hot60": hot60,
            "hot120": hot120,
            "hot_tails": hot_tails,
        },
        "pattern": {
            "repeat": repeat,
            "brother": brother,
            "diagonal": diagonal,
            "difference": difference,
            "missing": missing[:30],
        },
        "ranked": ranked[:30],
        "recommend": {
            "top20": top20,
            "top10": top10,
            "five_star": top20[:5],
            "four_star": top20[:4],
            "three_star": top20[:3],
            "super_candidates": super_candidates,
            "confidence": min(99, 65 + len([x for x in ranked[:10] if x["score"] >= 70])),
        },
    }