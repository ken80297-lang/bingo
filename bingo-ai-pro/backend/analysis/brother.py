BROTHER_NUMBERS = [11, 22, 33, 44, 55, 66, 77]


def analyze(draws):
    if not draws:
        return {
            "title": "雙生號",
            "description": "沒有資料",
            "numbers": []
        }

    latest = set(draws[0]["numbers"])

    matched = []

    for number in BROTHER_NUMBERS:
        if number in latest:
            matched.append(number)

    return {
        "title": "雙生號",
        "description": "本期開出的雙生號",
        "numbers": matched,
        "count": len(matched)
    }