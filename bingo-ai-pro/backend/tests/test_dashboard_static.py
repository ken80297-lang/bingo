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
    assert "AI 信心" in html
    assert "AI 高機率 5 號" in html
    assert "AI 推薦 20 碼" in html
    assert "AI 超級獎候選" in html
    assert "趨勢摘要" in html
    assert "大小 / 單雙" in html
    assert "正式資料" in html
    assert "老玩家分析" in html
    assert "上一期推薦結果" in html
    assert "歷史推薦紀錄" in html
    assert "AI推薦10碼" not in html
    assert "Prediction History" not in html
    assert "AI 推薦原因" not in html
    assert "release_version" in html
    assert "git_commit_short" in html
    assert "phase" in html
    assert "production_generation" in html
    assert "production_start_issue" in html
    assert "Phase 28" not in html
    assert "v28.0.0" not in html
    assert "Production Generation" not in html
    assert "Production Start Issue" not in html
    assert "115040780" not in html
    assert "Model / Feature" not in html


def test_dashboard_collapsible_sections_default_closed():
    html = _html()

    assert 'aria-controls="ruleLibraryBody"' in html
    assert 'aria-controls="reasonsBody"' in html
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
