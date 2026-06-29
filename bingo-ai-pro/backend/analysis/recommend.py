from collections import Counter


def build_recommendation(draws: list[dict], analysis: dict) -> dict:
    hot = [int(num) for num in analysis.get("hot", {}).get("hot_numbers", [])]
    cold = [int(num) for num in analysis.get("cold", {}).get("cold_numbers", [])]
    repeat = [int(num) for num in analysis.get("repeat", {}).get("repeat_numbers", [])]
    missing = [int(num) for num in analysis.get("missing", {}).get("missing_numbers", [])]

    all_numbers = [num for draw in draws[:100] for num in draw["numbers"]]
    frequency = Counter(all_numbers)
    top_numbers = [num for num, _ in frequency.most_common(8)]
    recommendation = sorted(set(hot[:3] + repeat[:2] + missing[:2] + top_numbers[:3]))[:10]

    return {
        "title": "今日推薦",
        "description": "綜合熱號、重號、補號與出現頻率挑選推薦號碼",
        "recommendation_numbers": [f"{num:02d}" for num in recommendation],
    }
