from pathlib import Path
import subprocess


def test_new_official_draw_survives_prediction_handoff_snapshot():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync('backend/static/dashboard.html', 'utf8');
const start = html.indexOf('    function render(data, errors)');
const end = html.indexOf('    function toggleSection', start);
const numbers = Array.from({length: 20}, (_, i) => i + 1);
const oldDraw = {issue: '115056610', raw_numbers: numbers};
const oldPrediction = {prediction_issue: '115056611', based_on_issue: '115056610', raw_candidates: numbers, super_number: 7};
let rendered;
const context = {
  normalizeOfficialDraw: () => ({issue: '115056612', raw_numbers: numbers}),
  normalizeNext: () => ({raw_candidates: []}),
  hasValidNumberSet: values => values.length === 20,
  issueNumber: value => Number(value) || null,
  loadCardOneSnapshot: () => ({officialDraw: oldDraw, next: oldPrediction}),
  saveCardOneSnapshot() {}, isCardOnePredictionCompatible: () => false,
  normalizeCardTwo: () => ({}), normalizeCardThree: () => ({}),
  document: {getElementById: () => ({style: {}, textContent: ''})},
  renderNext: (next, official) => {rendered = {next, official};},
  renderCardTwo() {}, renderCardThree() {}, console
};
vm.createContext(context);
vm.runInContext(html.slice(start, end), context);
context.render({}, []);
assert.equal(rendered.official.issue, '115056612');
assert.equal(rendered.next.prediction_issue, '115056611');
assert.equal(rendered.next.super_number, 7);
assert.equal(rendered.next.handoff_pending, true);
context.normalizeOfficialDraw = () => ({raw_numbers: []});
context.render({}, []);
assert.equal(rendered.official.issue, '115056610');
"""
    result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[2], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
