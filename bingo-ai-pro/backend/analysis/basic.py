def analyze(draws):
    total_big = 0
    total_small = 0
    total_odd = 0
    total_even = 0

    for draw in draws:
        nums = draw["numbers"]

        big = sum(1 for n in nums if n >= 41)
        small = 20 - big

        odd = sum(1 for n in nums if n % 2 == 1)
        even = 20 - odd

        total_big += big
        total_small += small
        total_odd += odd
        total_even += even

    count = len(draws)

    return {
        "big_avg": round(total_big / count, 2),
        "small_avg": round(total_small / count, 2),
        "odd_avg": round(total_odd / count, 2),
        "even_avg": round(total_even / count, 2),
    }
