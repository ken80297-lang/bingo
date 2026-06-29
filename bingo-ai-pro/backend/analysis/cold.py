from collections import Counter


def analyze_cold(draws: list[dict]) -> dict:
    counts = Counter(number for draw in draws[:100] for number in draw["numbers"])
    all_numbers = list(range(1, 81))
    cold_list = sorted(all_numbers, key=lambda num: (counts.get(num, 0), num))[:10]
    return {
        "title": "冷號",
        "description": "最近 100 期最少出現的號碼",
        "cold_numbers": cold_list,
        "frequency": {str(number): counts.get(number, 0) for number in cold_list},
    }
