"""Regressions for blockers found at PR181 2afd142."""
from database import prediction_history_store as store
from services.top_k_shadow_evaluation import evaluate_frozen_prediction


def test_standard_select_contains_frozen_metadata():
    assert 'fast_path_metadata' in store.PREDICTION_SELECT_COLUMNS


def test_top6_must_have_exactly_six_numbers():
    record = {'issue': '115052199', 'prediction_issue': '115052200',
              'prediction_status': 'verified', 'verified_issue': '115052200',
              'recommend_numbers': list(range(1,21)), 'winning_numbers': list(range(1,21)),
              'fast_path_metadata': {'top_k_shadow': {'source_issue': '115052199',
                 'target_issue': '115052200', 'top5': [1,2,3,4,5], 'top6': [1,2,3,4,5]}}}
    assert evaluate_frozen_prediction(record)['status'] == 'unavailable'
