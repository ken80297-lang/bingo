from collections import Counter


def analyze_odd_even(draws: list[dict]) -> dict:
    counts = Counter("odd" if number % 2 else "even" for draw in draws[:120] for number in draw["numbers"])
    odd = counts["odd"]
    even = counts["even"]
    ratio = f"{odd}:{even}"
    bias = "單偏" if odd > even else "雙偏" if even > odd else "平衡"
    return {
        "title": "單雙比例",
        "description": "最近 120 期單雙比例",
        "ratio": ratio,
        "bias": bias,
    }
