import copy
import pytest

from services.top_k_shadow_evaluation import freeze_selection, evaluate_frozen_prediction, snapshot_hash


def test_v2_exact_scores_roundtrip_and_tampering():
    scores = {n: n / 7 for n in range(1,21)}
    shadow = freeze_selection(list(range(1,21)), scores, '115052199', '115052200')
    assert shadow['ranking_scores']['19'] == scores[19]
    record = {'issue': '115052199', 'prediction_issue': '115052200',
              'prediction_status': 'verified', 'verified_issue': '115052200',
              'recommend_numbers': list(range(1,21)), 'winning_numbers': list(range(16,36)),
              'fast_path_metadata': {'top_k_shadow': shadow}}
    assert evaluate_frozen_prediction(record)['top6_hits'] == 5
    altered = copy.deepcopy(record)
    altered['fast_path_metadata']['top_k_shadow']['ranking_scores']['1'] = 100
    assert evaluate_frozen_prediction(altered)['status'] == 'unavailable'


@pytest.mark.parametrize('value', ['1', 'true', 'yes', 'on', 'TRUE'])
def test_one_shot_core_cron_never_constructs_delayed_worker(monkeypatch, value):
    from services import shadow_dynamic_observer as worker
    monkeypatch.setenv('AI_LIFECYCLE_CRON_CORE_ONLY', value)
    def forbidden():
        raise AssertionError('delayed executor must not be created')
    monkeypatch.setattr(worker, '_shadow_generate_executor', forbidden)
    assert worker.generate_for_prediction_async({}, {}) == {'status': 'skipped', 'reason': 'cron_core_only'}


def test_ranking_generation_preserves_production_selection_and_weights(monkeypatch):
    from services import recommendation_center as center, model_engine
    from services import top_k_shadow_evaluation as helpers
    monkeypatch.setattr(center, 'get_prediction_history_statistics', lambda *a: {})
    monkeypatch.setattr(model_engine, 'run_all_models', lambda *a, **kw: {
        'models': [{'model': 'laowanjia', 'confidence': 60, 'candidate_numbers': list(range(1,21))}]})
    context = {'analysis_record': {'issue': '115052199', 'numbers': list(range(1,21))},
               'active_adaptive_weights': {'strategy': 'v7_models', 'laowanjia_weight': 1.1},
               'shadow_rule_promotion_snapshot': {'rules': {}},
               'previous_recommend_numbers': [], 'learning_analysis_history': []}
    original = copy.deepcopy(context)
    enabled = center.calculate_fast_recommendation('115052199', '115052200', context)
    assert enabled['status'] == 'ok'
    shadow = enabled['recommendation']['top_k_shadow']
    assert shadow['version'] == 'top-k-shadow-v2'
    assert shadow['snapshot_sha256'] == snapshot_hash(shadow)
    def unavailable(*a, **kw):
        raise ValueError('simulate shadow-only failure')
    monkeypatch.setattr(helpers, 'freeze_selection', unavailable)
    disabled = center.calculate_fast_recommendation('115052199', '115052200', context)
    for field in ('results', 'confidence', 'model_scores', 'super_recommendation', 'production_fast_path'):
        assert enabled['recommendation'][field] == disabled['recommendation'][field]
    assert context == original
