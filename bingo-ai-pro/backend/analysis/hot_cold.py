from collections import Counter


def analyze(draws):
    counter = Counter()

    for draw in draws:
        counter.update(draw["numbers"])

    hot = [
        n for n, _ in sorted(
            counter.items(),
            key=lambda x: x[1],
            reverse=True
        )[:10]
    ]

    cold = [
        n for n, _ in sorted(
            counter.items(),
            key=lambda x: x[1]
        )[:10]
    ]

    return {
        "hot": hot,
        "cold": cold,
        "count": dict(counter)
    }