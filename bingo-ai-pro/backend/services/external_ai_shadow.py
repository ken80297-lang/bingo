"""Fail-open external AI shadow recommendation client.

No calls are made unless EXTERNAL_AI_SHADOW_ENABLED=1 and an API key is set.
Never use this output as a production prediction without independent validation.
"""
from __future__ import annotations

import json
import os
from urllib import request, error


def propose_shadow_numbers(draw: dict, baseline: dict, *, timeout: float = 6.0) -> dict:
    """Return validated candidate numbers or a non-fatal skipped/error result."""
    if os.getenv("EXTERNAL_AI_SHADOW_ENABLED", "0") != "1":
        return {"status": "skipped", "reason": "disabled"}
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        return {"status": "skipped", "reason": "missing_api_key"}
    issue = str(draw.get("issue", ""))
    if not issue.isdigit():
        return {"status": "skipped", "reason": "invalid_issue"}
    prompt = {
        "task": "Suggest shadow-only Bingo candidates; no claims of predictive advantage.",
        "rules": "Return JSON object with numbers (20 distinct integers 1-80), top5 (5 distinct integers selected from numbers), super_number (one integer 1-80).",
        "latest_draw": {"issue": issue, "numbers": draw.get("numbers", []), "super_number": draw.get("super_number")},
        "baseline": baseline,
    }
    payload = json.dumps({
        "model": os.getenv("GROQ_SHADOW_MODEL", "llama-3.3-70b-versatile"),
        "temperature": 0,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": "You are a lottery research assistant. Output only valid JSON. Lottery draws are random; do not promise improved odds."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)},
        ],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            response = json.load(resp)
        answer = json.loads(response["choices"][0]["message"]["content"])
        numbers = answer["numbers"]
        top5 = answer["top5"]
        super_number = answer["super_number"]
        valid = (
            isinstance(numbers, list) and len(numbers) == 20
            and all(type(n) is int and 1 <= n <= 80 for n in numbers)
            and len(set(numbers)) == 20
            and isinstance(top5, list) and len(top5) == 5
            and all(type(n) is int and n in numbers for n in top5)
            and len(set(top5)) == 5
            and type(super_number) is int and 1 <= super_number <= 80
        )
        if not valid:
            return {"status": "error", "reason": "invalid_model_output", "issue": issue}
        return {"status": "ok", "issue": issue, "numbers": sorted(numbers), "top5": sorted(top5), "super_number": super_number, "provider": "groq"}
    except (error.HTTPError, error.URLError, TimeoutError, ValueError, KeyError, TypeError, OSError) as exc:
        return {"status": "error", "reason": type(exc).__name__, "issue": issue}
