from collections import Counter

PAIRS = [(11, 22), (22, 33), (33, 44), (44, 55), (55, 66), (66, 77)]


def analyze_pair(draws: list[dict]) -> dict:
    counts = Counter(number for draw in draws[:100] for number in draw["numbers"])
    pair_scores = [pair for pair in PAIRS if counts.get(pair[0], 0) and counts.get(pair[1], 0)]
    return {
        "title": "雙生號",
        "description": "經常成對出現的雙生號",
        "pairs": [[f"{a:02d}", f"{b:02d}"] for a, b in pair_scores],
    }
