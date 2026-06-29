def analyze(draws):
    if len(draws) < 2:
        return {"title": "重號", "description": "資料不足", "repeat_numbers": []}

    last_numbers = set(draws[0]["numbers"])
    previous_numbers = set(draws[1]["numbers"])
    repeat_numbers = sorted(last_numbers & previous_numbers)
    return {
        "title": "重號",
        "description": "上一期與前一期重複出現的號碼",
        "repeat_numbers": [f"{num:02d}" for num in repeat_numbers],
    }
