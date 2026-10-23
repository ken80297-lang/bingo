import copy
from scripts.validate_top_k_readonly import audit
from services.top_k_shadow_evaluation import freeze_selection, snapshot_hash


def sample():
    before = {'id': 1, 'issue': '115052199', 'prediction_issue': '115052200',
              'predict_time': '2026-10-22T08:00:00+08:00', 'created_at': '2026-10-22T08:00:01+08:00',
              'prediction_status': 'waiting_draw', 'recommend_numbers': list(range(1, 21)),
              'model_scores': {}, 'fast_path_metadata': {'top_k_shadow': {
                  'source_issue': '115052199', 'target_issue': '115052200',
                  'top5': [1,2,3,4,5], 'top6': [1,2,3,4,5,6]}}}
    after = copy.deepcopy(before)
    shadow = freeze_selection(list(range(1,21)), {n: -float(n) for n in range(1,21)}, '115052199', '115052200')
    shadow['frozen_at'] = '2026-10-22T08:00:00+08:00'
    shadow['snapshot_sha256'] = snapshot_hash(shadow)
    before['fast_path_metadata']['top_k_shadow'] = shadow
    after = copy.deepcopy(before)
    after.update(prediction_status='verified', verified_issue='115052200',
                 winning_numbers=list(range(4,24)), verified_at='2026-10-22T08:06:00+08:00')
    return {'plan_locked_at': '2026-10-21T00:00:00+08:00', 'oos_start_issue': '115052200',
            'pairs': [{'before': before, 'after': after, 'captured_at': '2026-10-22T08:01:00+08:00',
                       'target_draw_at': '2026-10-22T08:05:00+08:00',
                       'official': {'issue': '115052200', 'numbers': list(range(4,24))}}]}


def test_audit_scores_without_mutating_input():
    data = sample()
    original = copy.deepcopy(data)
    result = audit(data)
    assert result['eligible_n'] == 1
    assert result['top5']['mean_hits'] == 2
    assert result['top6']['mean_hits'] == 3
    assert result['top5']['random_within_same_20'] == 4.25
    assert data == original


def test_postdraw_capture_and_replaced_selection_excluded():
    data = sample()
    data['pairs'][0]['captured_at'] = '2026-10-22T08:07:00+08:00'
    assert audit(data)['eligible_n'] == 0
    data = sample()
    data['pairs'][0]['after']['fast_path_metadata']['top_k_shadow']['top5'][0] = 9
    assert audit(data)['eligible_n'] == 0


def test_duplicates_and_empty_data_cannot_support_performance():
    data = sample()
    data['pairs'] *= 2
    assert audit(data)['eligible_n'] == 0
    assert audit({'pairs': []})['top5']['mean_hits'] is None


def test_cannot_replace_ranking_scores_even_with_recomputed_hash():
    data = sample()
    for side in ('before', 'after'):
        shadow = data['pairs'][0][side]['fast_path_metadata']['top_k_shadow']
        shadow['ranking_scores']['20'] = 100
        shadow['snapshot_sha256'] = snapshot_hash(shadow)
    assert audit(data)['eligible_n'] == 0


def test_legacy_snapshot_not_promoted_to_reproducible_oos():
    data = sample()
    data['pairs'][0]['before']['fast_path_metadata']['top_k_shadow']['version'] = 'top-k-shadow-v1'
    assert audit(data)['eligible_n'] == 0
