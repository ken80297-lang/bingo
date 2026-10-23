from copy import deepcopy
import random
import pytest

from services.top_k_shadow_v3 import freeze_v3, validate_v3, patch_candidates, scores_from_evidence, MODELS
from services.top_k_shadow_evaluation import snapshot_hash


def history():
    rows = []
    for index in range(100):
        numbers = sorted(random.Random(index).sample(range(1, 81), 20))
        rows.append({'issue': str(115059900-index), 'numbers': numbers,
                     'patch_numbers': list(range(1, 13)), 'twins': [numbers[:2]],
                     'consecutive': [numbers[2:5]], 'hot_numbers': numbers[:10],
                     'missing_numbers': numbers[10:], 'laowanjia_score': 70,
                     'cluster_score': 70, 'pattern': '補號模式 連號模式'})
    return rows


def frozen():
    return freeze_v3(list(range(1, 21)), history(), {m: 1 for m in MODELS}, '115059900', '115059901')


def test_freeze_evidence_roundtrip_is_pure_and_replayable():
    rows = history(); before = deepcopy(rows)
    snapshot = freeze_v3(list(range(1, 21)), rows, {m: 1 for m in MODELS}, '115059900', '115059901')
    assert validate_v3(snapshot, list(range(1, 21)), '115059900', '115059901')
    assert rows == before
    assert snapshot['top6'][:5] == snapshot['top5']
    assert set(snapshot['top6']) <= set(range(1, 21))
    assert snapshot['snapshot_sha256'] == snapshot_hash(snapshot)
    assert len(snapshot['model_evidence']) == 5


@pytest.mark.parametrize('kind', ['future', 'missing', 'duplicate'])
def test_rejects_bad_history_before_any_fallback_reads(kind):
    rows = history()
    if kind == 'future': rows[0]['issue'] = '115059901'
    if kind == 'missing': rows.pop()
    if kind == 'duplicate': rows[-1]['issue'] = rows[0]['issue']
    with pytest.raises(ValueError):
        freeze_v3(list(range(1, 21)), rows, {m: 1 for m in MODELS}, '115059900', '115059901')


def test_patch_is_order_independent_and_circular():
    numbers = history()[0]['numbers']
    assert patch_candidates(numbers, '100') == patch_candidates(numbers[::-1], '100')
    assert not set(patch_candidates(numbers, '100')) & set(numbers)


def test_score_has_no_position_bonus_and_detects_rehashed_tampering():
    snapshot = frozen()
    evidence = snapshot['model_evidence']
    expected = scores_from_evidence(evidence, list(range(1, 21)))
    changed = deepcopy(evidence)
    for model in changed.values(): model['candidate_numbers'].reverse()
    assert scores_from_evidence(changed, list(range(1, 21))) == expected
    snapshot['ranking_scores']['1'] += 1
    snapshot['snapshot_sha256'] = snapshot_hash(snapshot)
    with pytest.raises(ValueError): validate_v3(snapshot, list(range(1, 21)), '115059900', '115059901')


def test_live_output_invariance_on_off_and_shadow_failure(monkeypatch):
    from services import recommendation_center as center, top_k_shadow_v3 as v3
    monkeypatch.setenv('TOP_K_SHADOW_CAPTURE_ENABLED', 'true')
    monkeypatch.setattr(center, 'get_prediction_history_statistics', lambda *a: {})
    monkeypatch.setattr(center, '_load_super_numbers', lambda *a: [])
    rows = history()
    context = {'analysis_record': dict(rows[0]), 'active_adaptive_weights': {'strategy': 'v7_models'},
               'shadow_rule_promotion_snapshot': {'rules': {}},
               'previous_recommend_numbers': [], 'learning_analysis_history': rows}
    before = deepcopy(context)
    monkeypatch.setenv('TOP_K_SHADOW_V3_CAPTURE_ENABLED', '0')
    off = center.calculate_fast_recommendation('115059900', '115059901', context)['recommendation']
    monkeypatch.setenv('TOP_K_SHADOW_V3_CAPTURE_ENABLED', '1')
    on = center.calculate_fast_recommendation('115059900', '115059901', context)['recommendation']
    assert 'top_k_shadow_v3' in on
    def fail(*a, **kw): raise RuntimeError('research only failure')
    monkeypatch.setattr(v3, 'freeze_v3', fail)
    failed = center.calculate_fast_recommendation('115059900', '115059901', context)['recommendation']
    for field in ('results', 'confidence', 'model_scores', 'super_recommendation', 'production_fast_path'):
        assert off[field] == on[field] == failed[field]
    assert 'top_k_shadow_v3' not in failed
    assert before == context
