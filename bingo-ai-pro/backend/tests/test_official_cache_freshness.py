from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from collectors import taiwan_lottery_collector as collector


def test_realtime_requests_bypass_fixed_url_stale_draw_without_extra_fetch(monkeypatch):
    calls = []
    tokens = iter([1001, 1002])
    monkeypatch.setattr(collector.time, "time_ns", lambda: next(tokens))

    def cached_response(url, *, params, headers):
        calls.append((dict(params), dict(headers)))
        # Reproduce the observed CDN: the fixed URL has the previous draw.
        issue = 115058491 if "_" in params else 115058490
        return {"ok": True, "data": {"rtCode": 0, "content": {"bingoQueryResult": [{
            "drawTerm": issue, "bigShowOrder": list(range(1, 21)),
            "openShowOrder": list(range(1, 21)), "bullEyeTop": 20,
        }]}}}

    monkeypatch.setattr(collector, "safe_get_json", cached_response)
    assert collector.fetch_official_bingo_results("2026-10-16", page_size=1)[0]["issue"] == "115058490"
    for _ in range(2):
        assert collector.fetch_official_bingo_results("2026-10-16", page_size=1, fresh=True)[0]["issue"] == "115058491"
    assert len(calls) == 3
    assert calls[1][0] == {"openDate": "2026-10-16", "pageNum": 1, "pageSize": 1, "_": "1001"}
    assert calls[2][0]["_"] == "1002"
    assert calls[1][1]["Cache-Control"] == "no-cache"
