from collections import Counter


def analyze_super(draws: list[dict]) -> dict:
    counts = Counter(number for draw in draws[:100] for number in draw["numbers"])
    trending = [number for number, _ in counts.most_common(20) if number in {11, 22, 33, 44, 55, 66, 77}]
    return {
        "title": "超級獎號",
        "description": "熱門號與雙生號的超級獎號候選",
        "super_candidates": [f"{num:02d}" for num in trending[:5]],
    }
