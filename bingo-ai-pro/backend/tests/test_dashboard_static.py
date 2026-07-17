from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD_HTML = ROOT / "static" / "dashboard.html"


def _html() -> str:
    return DASHBOARD_HTML.read_text(encoding="utf-8")


def test_dashboard_release_candidate_copy_and_endpoints():
    html = _html()

    assert "/api/pipeline/health" in html
    assert "下一期 AI 推薦" in html
    assert "AI推薦20碼" in html
    assert "老玩家分析" in html
    assert "上一期命中結果" in html
    assert "歷史推薦紀錄" in html
    assert "AI推薦10碼" not in html
    assert "Prediction History" not in html
    assert "AI 推薦原因" not in html


def test_dashboard_collapsible_sections_default_closed():
    html = _html()

    assert 'aria-controls="modelVotesBody"' in html
    assert 'aria-controls="nextRuleLibraryBody"' in html
    assert 'aria-controls="ruleLibraryBody"' in html
    assert 'aria-controls="reasonsBody"' in html
    assert '<div id="modelVotesBody" hidden>' in html
    assert '<div id="nextRuleLibraryBody" hidden>' in html
    assert '<div id="ruleLibraryBody" hidden>' in html
    assert '<div id="reasonsBody" hidden>' in html


def test_dashboard_formats_twenty_number_history_and_production_only():
    html = _html()

    assert "production_valid !== false" in html
    assert "numberLabel" in html
    assert "padStart(2, \"0\")" in html
    assert "命中" in html
    assert "/ 20" in html
    assert "尚無資料" in html
