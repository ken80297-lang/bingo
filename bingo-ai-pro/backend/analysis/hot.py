from collections import Counter


def analyze_hot(draws: list[dict]) -> dict:
    freq = Counter(number for draw in draws[:100] for number in draw["numbers"])
    hot_numbers = [number for number, _ in freq.most_common(10)]
    return {
        "title": "熱號",
        "description": "最近 100 期最常出現的號碼",
        "hot_numbers": hot_numbers,
        "frequency": {str(number): count for number, count in freq.most_common(10)},
    }
