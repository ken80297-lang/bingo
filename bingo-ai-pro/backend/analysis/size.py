from collections import Counter


def analyze_size(draws: list[dict]) -> dict:
    counts = Counter("small" if number <= 40 else "big" for draw in draws[:120] for number in draw["numbers"])
    total = sum(counts.values())
    small = counts["small"]
    big = counts["big"]
    ratio = f"{small}:{big}"
    bias = "小偏" if small > big else "大偏" if big > small else "平衡"
    return {
        "title": "大小比例",
        "description": "最近 120 期大小出現比例",
        "ratio": ratio,
        "bias": bias,
    }
