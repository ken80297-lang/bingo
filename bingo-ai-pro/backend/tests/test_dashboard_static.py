from __future__ import annotations

import json
import pathlib
import subprocess
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


def _script() -> str:
    html = _html()
    start = html.index("<script>")
    end = html.index("</script>")
    return html[start:end]


def _run_card1_vm_scenario(scenario: str) -> str:
    script = rf"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('backend/static/dashboard.html', 'utf8');
const code = html.match(/<script>([\s\S]*)<\/script>/)[1];
const elements = new Map();
function el(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id, innerHTML: '', textContent: '', hidden: false, style: {{}}, attrs: {{}},
      addEventListener() {{}},
      getAttribute(name) {{ return this.attrs[name] || 'false'; }},
      setAttribute(name, value) {{ this.attrs[name] = String(value); }},
      querySelector() {{ return {{ textContent: '' }}; }}
    }});
  }}
  return elements.get(id);
}}
['refreshButton','refreshState','errorBox','cardNext','cardTwo','cardReasons','cardAlerts','cardHistory','cardSync','cardWake','cardContinuity','cardPipeline','cardOfficial','cardOperations','cardEvolution','developerInfo'].forEach(el);
const context = {{
  console, window: {{}}, setInterval: () => 1, clearInterval() {{}}, setTimeout: () => 1,
  fetch: async () => ({{ ok: true, json: async () => ({{}}) }}),
  localStorage: {{ setItem() {{}}, getItem() {{ return null; }} }},
  document: {{ visibilityState: 'hidden', addEventListener() {{}}, getElementById: el }},
  Intl, Date, JSON, Number, String, Array, Boolean, Math, RegExp
}};
vm.createContext(context);
vm.runInContext(code, context);
function rules() {{
  return ['hot','cold','missing','repeat','tail','gap','cluster','diagonal','super','laowanjia','ladder','partial_ladder','extended_ladder','reverse','neighbor','guide','integrated','sunset','momentum']
    .map((key, index) => ({{ key, name: key, status: 'ready', score: 80 - index, candidate_numbers: [21 + (index % 20)] }}));
}}
function payload(targetIssue) {{
  const officialNumbers = Array.from({{ length: 20 }}, (_, i) => i + 1);
  const predictionNumbers = Array.from({{ length: 20 }}, (_, i) => i + 21);
  return {{
    playerSummary: {{
      status: 'ok',
      latest_official_draw: {{ issue: String(targetIssue - 1), draw_time: '2026-07-30T12:00:00+08:00', numbers: officialNumbers, super_number: 5, verification_status: 'verified' }},
      next_prediction: {{
        prediction_issue: String(targetIssue), based_on_issue: String(targetIssue - 1), based_on_draw_time: '2026-07-30T12:00:00+08:00',
        candidates: predictionNumbers, high_probability_numbers: predictionNumbers.slice(0, 5), super_candidates: predictionNumbers.slice(0, 3),
        size_prediction: {{ label: 'balanced', small_count: 10, large_count: 10 }},
        odd_even_prediction: {{ label: 'balanced', odd_count: 10, even_count: 10 }},
        confidence_percent: 88, generated_at: '2026-07-30T12:01:30+08:00', rule_library: {{ rules: rules() }}, reasons: [], alerts: {{}}
      }},
      card_two: {{ available: true, title: 'Card Two', status_text: 'final', prediction_numbers: predictionNumbers, matched_numbers: [], rules: [] }},
      history: {{}}, previous_verification: {{}}, current_draw: {{}}, data_counts: {{}}, sync: {{}}, prediction_history: []
    }}
  }};
}}
context.render(payload(101), []);
const first = el('cardNext').innerHTML;
const next = payload(102);
const mode = '{scenario}';
if (mode === 'missing_size') delete next.playerSummary.next_prediction.size_prediction;
if (mode === 'missing_confidence') delete next.playerSummary.next_prediction.confidence_percent;
if (mode === 'missing_high_probability') delete next.playerSummary.next_prediction.high_probability_numbers;
if (mode === 'missing_odd_even') delete next.playerSummary.next_prediction.odd_even_prediction;
if (mode === 'missing_generated_at') delete next.playerSummary.next_prediction.generated_at;
if (mode === 'unverified_official') next.playerSummary.latest_official_draw.verification_status = 'pending';
if (mode === 'source_issue_mismatch') next.playerSummary.next_prediction.based_on_issue = '100';
if (mode === 'missing_official_time') delete next.playerSummary.latest_official_draw.draw_time;
if (mode === 'missing_official_numbers') next.playerSummary.latest_official_draw.numbers = [1, 2, 3];
if (mode === 'missing_prediction_numbers') next.playerSummary.next_prediction.candidates = [21, 22, 23];
if (mode === 'fast_path_pending_empty') {{
  next.playerSummary.partial = true;
  next.playerSummary.stale = true;
  next.playerSummary.latest_official_draw = null;
  next.playerSummary.current_draw = null;
  next.playerSummary.next_prediction = {{
    status: 'prediction_pending',
    prediction_issue: '115040999',
    recommend_numbers: [],
    high_probability_numbers: [],
    confidence_percent: 0
  }};
}}
context.render(next, []);
const second = el('cardNext').innerHTML;
console.log(JSON.stringify({{
  updated: second !== first,
  has102: second.includes('102'),
  hasVerified: second.includes('🟢 官方已驗證'),
  hasPendingStatus: second.includes('尚未確認'),
  hasBasedOnIssue: second.includes('依據期號'),
  hasOfficialTime: second.includes('2026/07/30') && second.includes('開獎時間'),
  hasBasedOnTime: second.includes('依據時間') && second.includes('2026/07/30'),
  hasGeneratedAt: second.includes('推薦建立時間') && second.includes('12:01'),
  hasRuleSnapshotHidden: second.includes('id="card1RuleSnapshotBody" hidden'),
  hasHighProbability: second.includes('高機率五個'),
  hasSize: second.includes('大小預測'),
  hasOddEven: second.includes('單雙預測'),
  hasConfidence: second.includes('AI 信心'),
  hasWaitingOfficial: second.includes('Waiting official verification'),
  hasWaitingRecommendation: second.includes('Waiting recommendation'),
  hasCardHtml: second.trim().length > 0
}}));
"""
    return subprocess.check_output(
        ["node", "-e", script],
        cwd=ROOT.parent,
        text=True,
        encoding="utf-8",
    ).strip()


def test_dashboard_release_candidate_copy_and_endpoints():
    html = _html()
    card1 = _card1_renderer()

    assert "/api/pipeline/health" in html
    assert "🎯 最新開獎與 AI 推薦" in html
    assert "最新官方開獎" in html
    assert "官方開獎 20 碼" in html
    assert "開獎時間" in html
    assert "預測期號" in html
    assert "依據期號" not in card1
    assert "依據時間" in html
    assert "推薦建立時間" in html
    assert "下一期 AI 推薦" in html
    assert "AI 信心" in html
    assert "AI 推薦 20 碼" in html
    assert "高機率五個" in html
    assert "大小預測" in html
    assert "單雙預測" in html
    assert "查看推薦依據" in html
    assert "推薦依據" in html
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
    assert html.count("🟢 官方已驗證") <= 1
    assert "Card 1 template is UI-frozen." not in card1
    assert "尚未確認" not in card1
    assert "Prediction 狀態" not in card1
    assert "同步狀態" not in card1
    assert "正式資料" not in card1
    assert "資料檢查中" not in card1
    assert "趨勢摘要" not in card1
    assert "單雙 / 大小" not in card1
    assert "AI 精選" not in card1
    assert "資料來源" not in card1
    assert "台彩官方" not in card1
    assert "已完成官方驗證" not in card1
    assert "驗證狀態：verified" not in card1
    assert "Source" not in card1
    assert "Provider" not in card1
    assert "raw JSON" not in card1
    assert "rule_key" not in card1
    assert "None" not in card1
    assert "null" not in card1
    assert "officialStatusText(officialStatus)" in card1

    expected_order = [
        "最新官方開獎",
        "下一期 AI 推薦",
        "renderConfidence(next)",
        "預測期號",
        "依據時間",
        "recommendationCreatedMetric",
        "AI 推薦 20 碼",
        "renderCompactHighProbability(next, numbers)",
        "大小預測",
        "單雙預測",
        "查看推薦依據",
    ]
    card1_template = card1[card1.index("const cardHtml") :]
    positions = [card1_template.index(token) for token in expected_order]
    assert positions == sorted(positions)


def test_dashboard_collapsible_sections_default_closed():
    html = _html()

    assert 'aria-controls="ruleLibraryBody"' in html
    assert 'aria-controls="reasonsBody"' in html
    assert 'aria-controls="card1RuleSnapshotBody"' in html
    assert '<div id="ruleLibraryBody" hidden>' in html
    assert '<div id="reasonsBody" hidden>' in html
    assert '<div id="card1RuleSnapshotBody" hidden>' in html


def test_dashboard_formats_twenty_number_history_and_production_only():
    html = _html()

    assert "production_valid !== false" in html
    assert "numberLabel" in html
    assert "padStart(2, \"0\")" in html
    assert "numberGrid" in html
    assert "normalizeOfficialDraw" in html
    assert "renderRuleSnapshotPreview" in html
    assert "CARD1_RULE_ORDER" in html
    assert "命中" in html
    assert "/ 20" in html
    assert "尚無資料" in html


def test_card_one_renders_fast_path_placeholders_without_blocking():
    html = _html()
    script = _script()
    card1 = _card1_renderer()

    assert "CARD_ONE_STORAGE_KEY" not in html
    assert "saveCardOneBrowserCache" not in html
    assert "loadCardOneBrowserCache" not in html
    assert "let cardOneFallbackHtml" in script
    assert "cardOneFallbackHtml = cardHtml" in card1
    assert "localStorage" not in card1

    assert "isCompleteOfficialDraw" in script
    assert 'normalizeOfficialStatus((officialDraw || {}).verification_status) === "verified"' in script
    assert "hasValidNumberSet((officialDraw || {}).raw_numbers, 20)" in script
    assert "numbers.includes(superNumber)" in script
    assert "hasDisplayableDateTime((officialDraw || {}).draw_time)" in script

    assert "isCompletePrediction" in script
    assert "targetIssue > sourceIssue" in script
    assert "isCardOneIssueAligned" in script
    assert "officialIssue === sourceIssue" in script
    assert "hasValidNumberSet((next || {}).raw_candidates, 20)" in script
    assert "Array.isArray(ruleLibrary.rules)" in script
    prediction_guard = script[script.index("function isCompletePrediction") : script.index("function isCardOneIssueAligned")]
    assert "raw_high_probability_numbers" not in prediction_guard
    assert "raw_super_candidates" not in prediction_guard
    assert "hasPredictionBalance" not in prediction_guard
    assert "confidence_available" not in prediction_guard
    assert "confidence_percent" not in prediction_guard

    assert "hasDisplayableHighProbability" in script
    assert "numbers.every((number) => predictionSet.has(number))" in script
    assert "renderPredictionBalance" in script
    assert "hasDisplayableConfidence" in script
    assert "renderConfidence" in script

    assert "const hasCompleteCardOne" not in card1
    assert "const hasOfficialNumbers = hasValidNumberSet((officialDraw || {}).raw_numbers, 20);" in card1
    assert "const hasPredictionNumbers = hasValidNumberSet((next || {}).raw_candidates, 20);" in card1
    assert "Waiting official verification" in card1
    assert "Waiting recommendation" in card1
    assert "if (!hasCompleteCardOne && card.innerHTML.trim()) return;" not in card1
    assert "if (cardOneFallbackHtml) card.innerHTML = cardOneFallbackHtml;" not in card1


def test_card_one_raw_fields_preserve_duplicate_and_subset_validation():
    script = _script()

    assert "raw_candidates: rawCandidates" in script
    assert "raw_high_probability_numbers: rawHighProbability" in script
    assert "raw_super_candidates: rawSuperCandidates" in script
    assert "raw_numbers: rawNumbers" in script
    assert "values.length === expectedCount" in script
    assert "normalizeNumbers(values).length === expectedCount" in script
    assert "numbers.length === raw.length" in script
    assert "numbers.every((number) => predictionSet.has(number))" in script


def test_card_one_failure_is_isolated_from_later_cards():
    script = _script()
    render_start = script.index("function render(data, errors)")
    render_end = script.index("function toggleSection")
    render_body = script[render_start:render_end]

    assert "try {" in render_body
    assert "renderNext(next, officialDraw);" in render_body
    assert 'console.warn("[dashboard] card one render failed", error);' in render_body
    assert render_body.index("renderCardTwo(cardTwo);") > render_body.index("catch (error)")


def test_card_one_updates_when_size_prediction_is_missing():
    result = json.loads(_run_card1_vm_scenario("missing_size"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasSize"] is False


def test_card_one_updates_when_confidence_is_missing():
    result = json.loads(_run_card1_vm_scenario("missing_confidence"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasConfidence"] is False


def test_card_one_updates_when_high_probability_is_missing():
    result = json.loads(_run_card1_vm_scenario("missing_high_probability"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasHighProbability"] is False


def test_card_one_updates_when_odd_even_prediction_is_missing():
    result = json.loads(_run_card1_vm_scenario("missing_odd_even"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasOddEven"] is False


def test_card_one_uses_verified_status_and_three_distinct_time_rows():
    result = json.loads(_run_card1_vm_scenario("complete"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasVerified"] is True
    assert result["hasPendingStatus"] is False
    assert result["hasBasedOnIssue"] is False
    assert result["hasOfficialTime"] is True
    assert result["hasBasedOnTime"] is True
    assert result["hasGeneratedAt"] is True
    assert result["hasRuleSnapshotHidden"] is True


def test_card_one_omits_recommendation_created_time_when_missing():
    result = json.loads(_run_card1_vm_scenario("missing_generated_at"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasGeneratedAt"] is False


def test_card_one_updates_when_official_is_not_verified():
    result = json.loads(_run_card1_vm_scenario("unverified_official"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasCardHtml"] is True


def test_card_one_updates_when_source_issue_mismatches_official_issue():
    result = json.loads(_run_card1_vm_scenario("source_issue_mismatch"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasCardHtml"] is True


def test_card_one_updates_when_official_time_is_missing():
    result = json.loads(_run_card1_vm_scenario("missing_official_time"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasCardHtml"] is True


def test_card_one_updates_with_placeholder_when_official_numbers_are_incomplete():
    result = json.loads(_run_card1_vm_scenario("missing_official_numbers"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasCardHtml"] is True
    assert result["hasWaitingOfficial"] is True


def test_card_one_updates_with_placeholder_when_prediction_numbers_are_incomplete():
    result = json.loads(_run_card1_vm_scenario("missing_prediction_numbers"))
    assert result["updated"] is True
    assert result["has102"] is True
    assert result["hasCardHtml"] is True
    assert result["hasWaitingRecommendation"] is True


def test_card_one_renders_fast_path_pending_empty_payload():
    result = json.loads(_run_card1_vm_scenario("fast_path_pending_empty"))
    assert result["updated"] is True
    assert result["hasCardHtml"] is True
    assert result["hasWaitingOfficial"] is True
    assert result["hasWaitingRecommendation"] is True
