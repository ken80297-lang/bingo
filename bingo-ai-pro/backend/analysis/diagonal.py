from collections import Counter


def diagonal_neighbors(number: int) -> set[int]:
    row, col = divmod(number - 1, 10)
    out = set()
    for dr, dc in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
        r, c = row + dr, col + dc
        if 0 <= r < 8 and 0 <= c < 10:
            out.add(r * 10 + c + 1)
    return out


def analyze_diagonal(draws: list[dict]) -> dict:
    if not draws:
        return {"title": "斜線", "description": "資料不足", "diagonal_candidates": []}

    last = draws[0]["numbers"]
    diagonal_counts = Counter()
    for number in last:
        diagonal_counts.update(diagonal_neighbors(number))

    candidates = [num for num, _ in diagonal_counts.most_common(8)]
    return {
        "title": "斜線",
        "description": "上一期號碼的 斜線鄰近號碼",
        "diagonal_candidates": [f"{num:02d}" for num in sorted(candidates)],
    }
