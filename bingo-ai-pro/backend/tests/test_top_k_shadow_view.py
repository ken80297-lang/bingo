from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from services.top_k_shadow_evaluation import freeze_selection, snapshot_hash
from services.top_k_shadow_view import build_shadow_view, load_shadow_view


def record():
    numbers = list(range(1, 21))
    shadow = freeze_selection(numbers, {n: 100 - n for n in numbers}, '100', '101')
    shadow['frozen_at'] = '2026-10-23T02:51:00+00:00'
    shadow['snapshot_sha256'] = snapshot_hash(shadow)
    return dict(issue='100', prediction_issue='101',
                created_at='2026-10-23T02:51:01+00:00',
                recommend_numbers=numbers, fast_path_metadata={'top_k_shadow': shadow},
                prediction_status='verified', verified_issue='101',
                verified_at='2026-10-23T02:56:00+00:00', winning_numbers=numbers,
                official_numbers=numbers, official_status='validated',
                official_draw_at='2026-10-23T02:55:00+00:00')


def test_verified_counts_baselines_and_no_private_score_exposure():
    source = record()
    original = deepcopy(source)
    result = build_shadow_view([source])
    assert result['verified_count'] == 1
    assert result['top5_mean'] == 5 and result['top6_mean'] == 6
    assert result['same20_top5_expected'] == 5
    assert result['same20_top6_expected'] == 6
    assert result['evidence'] == 'descriptive_only'
    assert 'ranking_scores' not in str(result)
    assert source == original


@pytest.mark.parametrize('mutation', ['hash', 'late', 'official', 'duplicate', 'early', 'rerank'])
def test_rejects_unverifiable_rows_without_zero_imputation(mutation):
    source = record()
    shadow = source['fast_path_metadata']['top_k_shadow']
    if mutation == 'hash': shadow['top5'][0] = 80
    if mutation == 'late': source['created_at'] = '2026-10-23T02:55:01+00:00'
    if mutation == 'official': source['official_numbers'] = list(range(21, 41))
    if mutation == 'early': source['verified_at'] = '2026-10-23T02:54:59+00:00'
    if mutation == 'rerank':
        shadow['ranking_scores']['20'] = 1000
        shadow['snapshot_sha256'] = snapshot_hash(shadow)
    result = build_shadow_view([source, deepcopy(source)] if mutation == 'duplicate' else [source])
    assert result['verified_count'] == 0
    assert result['top5_mean'] is None
    assert result['excluded'] == (2 if mutation == 'duplicate' else 1)


def test_pending_has_no_hit_score_even_when_official_draw_exists():
    source = record()
    source.update(prediction_status='waiting_draw', verified_at=None)
    result = build_shadow_view([source])
    assert result['waiting_count'] == 1
    assert result['items'][0]['top5_hits'] is None
    assert result['items'][0]['matched_numbers'] == []


def test_json_transport_integer_scores_restore_writer_float_hash():
    source = record()
    shadow = source['fast_path_metadata']['top_k_shadow']
    shadow['ranking_scores'] = {n: int(v) for n, v in shadow['ranking_scores'].items()}
    source.update(prediction_status='waiting_draw', official_draw_at=None, official_numbers=None)
    result = build_shadow_view([source])
    assert result['waiting_count'] == 1
    assert result['excluded'] == 0


def test_database_reader_has_readonly_transaction_and_bounded_query(monkeypatch):
    import database.postgres
    statements = []
    source = record()
    class Cursor:
        description = [SimpleNamespace(name=k) for k in source]
        def execute(self, query): statements.append(query)
        def fetchall(self): return [list(source.values())]
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Connection:
        @contextmanager
        def transaction(self): yield
        def cursor(self): return Cursor()
    @contextmanager
    def connection(): yield Connection()
    monkeypatch.setattr(database.postgres, 'dashboard_read_connection', connection)
    assert load_shadow_view()['verified_count'] == 1
    assert statements[0] == 'SET TRANSACTION READ ONLY'
    assert 'statement_timeout' in statements[1]
    assert 'LIMIT 200' in statements[2]
    assert all(word not in statements[2].upper() for word in ('INSERT', 'UPDATE', 'DELETE'))


def test_api_database_failure_is_503_without_secret_details(monkeypatch):
    import api.top_k_shadow as api
    def fail(): raise RuntimeError('secret database URL')
    monkeypatch.setattr(api, 'load_shadow_view', fail)
    with pytest.raises(HTTPException) as error:
        api.top_k_shadow(Response())
    assert error.value.status_code == 503
    assert 'secret' not in error.value.detail


def paired_record():
    from services.top_k_shadow_v3 import MODELS, VERSION, PROTOCOL, scores_from_evidence, rank_v3
    source = record()
    parent = source['recommend_numbers']
    evidence = {key: {'candidate_numbers': parent, 'confidence': 60.0, 'multiplier': 1.0} for key in MODELS}
    scores = scores_from_evidence(evidence, parent)
    snapshot = {**rank_v3(parent, scores, '100', '101'),
        'version': VERSION, 'protocol': PROTOCOL, 'source_issue': '100', 'target_issue': '101',
        'production_twenty': parent, 'ranking_scores': {str(n): v for n, v in scores.items()},
        'model_evidence': evidence, 'history_count': 100, 'history_first_issue': '1',
        'history_last_issue': '100', 'history_sha256': 'a'*64,
        'frozen_at': '2026-10-23T02:51:00+00:00', 'status': 'waiting_draw'}
    snapshot['snapshot_sha256'] = snapshot_hash(snapshot)
    source['fast_path_metadata']['top_k_shadow_v3'] = snapshot
    return source


def test_paired_statistics_use_only_matching_verified_parent_rows():
    source = paired_record()
    result = build_shadow_view([source])
    assert result['v3']['paired_verified_count'] == 1
    assert result['v3']['top6_mean'] == result['v3']['v2_paired_top6_mean'] == 6
    assert result['v3']['same20_expected'] == 6
    assert result['items'][0]['v3']['top6_hits'] == 6
    assert 'model_evidence' not in str(result)
    assert build_shadow_view([record()])['v3']['paired_verified_count'] == 0


@pytest.mark.parametrize('kind', ['hash', 'late', 'score'])
def test_bad_v3_does_not_remove_original_v2_result(kind):
    source = paired_record()
    snapshot = source['fast_path_metadata']['top_k_shadow_v3']
    if kind == 'hash': snapshot['top6'][0] = 80
    if kind == 'late': snapshot['frozen_at'] = '2026-10-23T02:55:01+00:00'
    if kind == 'score': snapshot['ranking_scores']['1'] += 1
    if kind != 'hash': snapshot['snapshot_sha256'] = snapshot_hash(snapshot)
    result = build_shadow_view([source])
    assert result['verified_count'] == 1
    assert result['v3']['excluded'] == 1
    assert result['v3']['paired_verified_count'] == 0


def test_pending_v3_does_not_use_available_draw_to_score_early():
    source = paired_record()
    source['prediction_status'] = 'waiting_draw'
    result = build_shadow_view([source])
    assert result['v3']['paired_verified_count'] == 0
    assert result['v3']['waiting_count'] == 1
    assert result['items'][0]['v3']['top6_hits'] is None
