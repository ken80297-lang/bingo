def analyze(draws):
    results = []

    for draw in draws:
        nums = sorted(draw["numbers"])
        pairs = []

        for i in range(len(nums) - 1):
            if nums[i + 1] == nums[i] + 1:
                pairs.append([nums[i], nums[i + 1]])

        results.append({
            "issue": draw.get("issue"),
            "pairs": pairs,
            "count": len(pairs)
        })

    latest = results[0] if results else None

    return {
        "latest": latest,
        "history": results
    }