from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD_HTML = ROOT / "static" / "dashboard.html"


def _html() -> str:
    return DASHBOARD_HTML.read_text(encoding="utf-8")


def _card1_renderer() -> str:
    html = _html()
    start = html.index("function renderNext")
    end = html.index("function renderCardTwo")
    return html[start:end]


def test_card_two_template_freeze():
    html = _html()

    assert '<article class="card" id="cardNext"></article>\n      <article class="card" id="cardTwo"></article>' in html
    assert html.count('id="cardTwo"') == 1
    assert 'id="cardLaowanjia"' not in html
    assert "📖 AI 驗證與分析報告" in html
    assert "驗證期號：" in html
    assert "最終分析" in html
    assert "AI 命中" in html
    assert "超級獎" in html
    assert "當期 AI 推薦" in html
    assert "查看詳細分析" in html
    assert "收合詳細分析" in html
    assert 'aria-controls="cardTwoReportBody"' in html
    assert '<div id="cardTwoReportBody" hidden>' in html
    assert "尚無已完成的最終分析報告" in html
    assert "AI 正在等待足夠的正式開獎與驗證資料。" in html
    assert "詳細規則分析" in html
    assert "預測號碼：" in html
    assert "實際開出：" in html
    assert "規則結果：" in html


def test_card_two_uses_isolated_css_classes():
    html = _html()
    css = html[html.index("<style>") : html.index("</style>")]

    for token in (
        ".card-two-shell",
        ".card-two-title",
        ".card-two-summary",
        ".card-two-number-hit",
        ".card-two-number-super",
        ".card-two-toggle",
        ".analysis-report-rule",
        ".analysis-report-field",
    ):
        assert token in css

    assert ".card span" not in css
    assert re.search(r"\.number\s*\{", css) is None
    assert ".badge" not in css


def test_card_two_frontend_states_and_card_one_boundary():
    html = _html()
    card1 = _card1_renderer()

    assert "function renderCardTwo" in html
    assert "function toggleCardTwoReport" in html
    assert "card-two-number-hit" in html
    assert "card-two-number-super" in html
    assert "saveCardTwoBrowserCache" in html
    assert "loadCardTwoBrowserCache" in html
    assert "escapeHtml" in html
    assert "cardTwoText(rule.summary" in html
    assert "None" not in card1
    assert "null" not in card1
    assert "cardTwo" not in card1
    assert "🎯 最新開獎與 AI 推薦" in card1
    assert "Card 1 template is UI-frozen." not in card1
    assert "預測期號" in card1
    assert "依據期號" not in card1
    assert "大小預測" in card1
    assert "單雙預測" in card1
    assert "Prediction 狀態" not in card1
