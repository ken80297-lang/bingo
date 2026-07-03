from collections import Counter, defaultdict


BROTHER_NUMBERS = {11, 22, 33, 44, 55, 66, 77}


def _tail(n: int) -> int:
    return n % 10


def _find_runs(numbers: list[int], min_len: int = 3) -> list[list[int]]:
    nums = sorted(numbers)
    runs = []
    current = [nums[0]]

    for n in nums[1:]:
        if n == current[-1] + 1:
            current.append(n)
        else:
            if len(current) >= min_len:
                runs.append(current)
            current = [n]

    if len(current) >= min_len:
        runs.append(current)

    return runs


def analyze_v2(draws: list[dict]) -> dict:
    if not draws:
        return {"status": "error", "message": "沒有資料"}

    latest = draws[0]
    latest_numbers = sorted(latest["numbers"])

    counter = Counter()
    super_counter = Counter()
    tail_counter = Counter()

    for draw in draws:
        nums = draw["numbers"]
        counter.update(nums)
        tail_counter.update([_tail(n) for n in nums])

        super_number = draw.get("super_number")
        if super_number:
            super_counter.update([super_number])

    hot = [n for n, _ in counter.most_common(10)]
    cold = [n for n, _ in counter.most_common()[-10:]]

    big = [n for n in latest_numbers if n >= 41]
    small = [n for n in latest_numbers if n <= 40]
    odd = [n for n in latest_numbers if n % 2 == 1]
    even = [n for n in latest_numbers if n % 2 == 0]

    consecutive_pairs = []
    for n in latest_numbers:
        if n + 1 in latest_numbers:
            consecutive_pairs.append([n, n + 1])

    runs_3 = _find_runs(latest_numbers, 3)
    runs_4 = [r for r in runs_3 if len(r) >= 4]
    runs_5 = [r for r in runs_3 if len(r) >= 5]

    brother = [n for n in latest_numbers if n in BROTHER_NUMBERS]

    diagonal = []
    for n in latest_numbers:
        if n + 9 in latest_numbers:
            diagonal.append([n, n + 9])
        if n + 11 in latest_numbers:
            diagonal.append([n, n + 11])

    repeat = []
    if len(draws) >= 2:
        repeat = sorted(set(draws[0]["numbers"]) & set(draws[1]["numbers"]))

    difference_candidates = defaultdict(set)

    if len(draws) >= 2:
        previous_numbers = draws[1]["numbers"]

        for n in previous_numbers:
            for diff in [1, -1, 9, -9, 10, -10, 11, -11]:
                candidate = n + diff
                if 1 <= candidate <= 80:
                    difference_candidates[str(diff)].add(candidate)

    difference = {
        key: sorted(value)
        for key, value in difference_candidates.items()
    }

    appeared = set()
    for draw in draws:
        appeared.update(draw["numbers"])

    missing = [n for n in range(1, 81) if n not in appeared]

    tail_rank = [
        {"tail": tail, "count": count}
        for tail, count in tail_counter.most_common()
    ]

    score = Counter()
    reasons: dict[int, list[str]] = defaultdict(list)

    for n in hot:
        score[n] += 15
        reasons[n].append("熱號")

    for n in cold:
        score[n] += 5
        reasons[n].append("冷號觀察")

    for n in repeat:
        score[n] += 18
        reasons[n].append("重號")

    for pair in diagonal:
        for n in pair:
            score[n] += 12
            reasons[n].append("斜線")

    for n in brother:
        score[n] += 10
        reasons[n].append("雙生號")

    for key, nums in difference.items():
        for n in nums:
            score[n] += 8
            reasons[n].append(f"相差{key}")

    for n in missing[:20]:
        score[n] += 6
        reasons[n].append("缺號補位")

    ranked = [
        {
            "number": n,
            "score": s,
            "reasons": reasons[n],
        }
        for n, s in score.most_common()
    ]

    recommend_20 = [item["number"] for item in ranked[:20]]
    recommend_10 = [item["number"] for item in ranked[:10]]
    five_star = recommend_20[:5]
    four_star = recommend_20[:4]
    three_star = recommend_20[:3]

    super_candidates = [
        n for n, _ in super_counter.most_common(5)
    ]

    if not super_candidates:
        super_candidates = recommend_20[:5]

    return {
        "status": "ok",
        "title": "老玩家 AI Pro V2",
        "issue": latest["issue"],
        "latest_numbers": latest_numbers,
        "basic": {
            "big_count": len(big),
            "small_count": len(small),
            "odd_count": len(odd),
            "even_count": len(even),
        },
        "hot": hot,
        "cold": cold,
        "repeat": repeat,
        "brother": {
            "numbers": brother,
            "count": len(brother),
        },
        "consecutive": {
            "pairs": consecutive_pairs,
            "runs_3": runs_3,
            "runs_4": runs_4,
            "runs_5": runs_5,
        },
        "diagonal": {
            "pairs": diagonal,
            "count": len(diagonal),
        },
        "difference": difference,
        "missing": missing[:30],
        "tail_rank": tail_rank,
        "ranked": ranked[:30],
        "recommend": {
            "top20": recommend_20,
            "top10": recommend_10,
            "five_star": five_star,
            "four_star": four_star,
            "three_star": three_star,
            "super_candidates": super_candidates,
            "confidence": min(99, 60 + len(recommend_20)),
        },
    }