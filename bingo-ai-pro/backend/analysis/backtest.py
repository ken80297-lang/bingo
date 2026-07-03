from analysis.laowanjia_v3 import analyze_v3


def run_backtest(draws: list[dict], issue: str | None = None) -> dict:
    if len(draws) < 30:
        return {"status": "error", "message": "資料不足，至少需要30期"}

    target_index = 0

    if issue:
        for i, draw in enumerate(draws):
            if str(draw["issue"]) == str(issue):
                target_index = i
                break
        else:
            return {"status": "error", "message": f"找不到期數 {issue}"}

    if target_index >= len(draws) - 30:
        return {"status": "error", "message": "目標期數之前資料不足，無法回測"}

    target_draw = draws[target_index]
    history_draws = draws[target_index + 1 :]

    prediction = analyze_v3(history_draws)
    if prediction.get("status") != "ok":
        return prediction

    actual_numbers = set(target_draw["numbers"])
    recommend = prediction["recommend"]

    top20_hits = sorted(actual_numbers & set(recommend["top20"]))
    top10_hits = sorted(actual_numbers & set(recommend["top10"]))
    five_star_hits = sorted(actual_numbers & set(recommend["five_star"]))
    four_star_hits = sorted(actual_numbers & set(recommend["four_star"]))
    three_star_hits = sorted(actual_numbers & set(recommend["three_star"]))

    actual_super = target_draw.get("super_number")
    super_candidates = recommend["super_candidates"]
    super_hit = actual_super in super_candidates if actual_super else False

    return {
        "status": "ok",
        "title": "老玩家 AI Pro 回測",
        "target_issue": target_draw["issue"],
        "predict_from_issue": prediction["issue"],
        "actual_numbers": sorted(target_draw["numbers"]),
        "actual_super_number": actual_super,
        "prediction": {
            "three_star": recommend["three_star"],
            "four_star": recommend["four_star"],
            "five_star": recommend["five_star"],
            "top10": recommend["top10"],
            "top20": recommend["top20"],
            "super_candidates": super_candidates,
            "confidence": recommend["confidence"],
        },
        "hits": {
            "three_star": {
                "count": len(three_star_hits),
                "numbers": three_star_hits,
                "all_hit": len(three_star_hits) == 3,
            },
            "four_star": {
                "count": len(four_star_hits),
                "numbers": four_star_hits,
                "all_hit": len(four_star_hits) == 4,
            },
            "five_star": {
                "count": len(five_star_hits),
                "numbers": five_star_hits,
                "all_hit": len(five_star_hits) == 5,
            },
            "top10": {
                "count": len(top10_hits),
                "numbers": top10_hits,
            },
            "top20": {
                "count": len(top20_hits),
                "numbers": top20_hits,
            },
            "super": {
                "hit": super_hit,
                "actual": actual_super,
                "candidates": super_candidates,
            },
        },
        "score": {
            "top20_hit_rate": round(len(top20_hits) / 20 * 100, 2),
            "top10_hit_rate": round(len(top10_hits) / 10 * 100, 2),
            "five_star_hit_rate": round(len(five_star_hits) / 5 * 100, 2),
            "four_star_hit_rate": round(len(four_star_hits) / 4 * 100, 2),
            "three_star_hit_rate": round(len(three_star_hits) / 3 * 100, 2),
        },
    }