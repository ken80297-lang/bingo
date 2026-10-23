"""Real save/read/verify SQL on temporary SQLite or explicitly isolated Postgres."""
import copy
import os
import sqlite3
import socket
import uuid
import pytest

from database import prediction_history_store as store
from services import prediction_service as service
from services.top_k_shadow_evaluation import freeze_selection, evaluate_frozen_prediction


@pytest.fixture(params=['sqlite', 'postgres'])
def isolated_database(request, monkeypatch, tmp_path):
    path = tmp_path / 'shadow.db'
    monkeypatch.setattr(store, '_sqlite_connection', lambda: sqlite3.connect(path))
    if request.param == 'sqlite':
        monkeypatch.setattr(store, '_cloud_enabled', lambda: False)
        yield 'sqlite', lambda: sqlite3.connect(path)
        return
    dsn = os.getenv('TOP_K_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('isolated Postgres DSN not supplied; no production DB attempted')
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    info = conninfo_to_dict(dsn)
    if info.get('host') not in ('127.0.0.1', 'localhost', '::1') or info.get('dbname') != 'top_k_validation':
        pytest.fail('Postgres integration only permits loopback / top_k_validation')
    schema = 'top_k_validation_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute('create schema ' + schema)
    def connect():
        return psycopg.connect(dsn, options='-csearch_path=' + schema, connect_timeout=3)
    monkeypatch.setattr(store, '_cloud_enabled', lambda: True)
    monkeypatch.setattr(store, '_cloud_connection', connect)
    monkeypatch.setattr(store, '_prediction_write_connection', connect)
    monkeypatch.setattr(store, '_dashboard_read_connection', connect)
    try:
        yield 'cloud', connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute('drop schema ' + schema + ' cascade')


def test_frozen_selection_survives_lifecycle(monkeypatch, isolated_database):
    backend, connect = isolated_database
    def no_network(*args, **kwargs):
        raise AssertionError('network is forbidden in isolated lifecycle validation')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setenv('AI_LIFECYCLE_CRON_CORE_ONLY', 'true')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_TYPE', 'sqlite')
    monkeypatch.setattr(store, '_INITIALIZED', False)
    monkeypatch.setattr(store, '_LATEST_PREDICTION_CACHE', {'payload': None})
    monkeypatch.setattr(store, '_PREDICTION_STATS_CACHE', {'payload': {}, 'expires_at': {}})
    monkeypatch.setattr(store, '_record_prediction_event', lambda **kw: None)
    monkeypatch.setattr(store, '_learned_prediction_issues', lambda: set())
    from services import operations_center
    monkeypatch.setattr(operations_center, 'record_operation_event', lambda **kw: None)
    assert store.init_prediction_history_tables()[backend] == 'available'
    sqlite_fallbacks = []
    if backend == 'cloud':
        def no_fallback():
            sqlite_fallbacks.append(True)
            raise AssertionError('Postgres validation may not silently fall back to SQLite')
        monkeypatch.setattr(store, '_sqlite_connection', no_fallback)
    service.shutdown_prediction_background_tasks(wait=True)
    service.reset_prediction_background_tasks_for_tests()
    monkeypatch.setattr(service, '_distributed_prediction_lock', lambda *a, **kw: ('isolated', None))
    monkeypatch.setattr(service, '_release_distributed_prediction_lock', lambda *a, **kw: None)
    monkeypatch.setattr(service, '_record_event', lambda **kw: None)
    from database import analysis_store
    monkeypatch.setattr(analysis_store, 'get_cached_analysis_history', lambda *a, **kw: ([], {}))
    from services import shadow_dynamic_observer, learning_engine
    monkeypatch.setattr(shadow_dynamic_observer, 'generate_for_prediction_async', lambda *a, **kw: {'status': 'skipped'})
    monkeypatch.setattr(learning_engine, 'ensure_live_prediction_snapshot_async', lambda *a, **kw: {'status': 'skipped'})
    monkeypatch.setattr(service, 'get_prediction_for_source_target', lambda *a, **kw: None)
    twenty = list(range(1, 21))
    frozen = freeze_selection(twenty, {n: float(n) for n in twenty}, '115052199', '115052200')
    payload = {'status': 'ok', 'recommendation_status': 'production_fast_path',
               'recommendation': {'issue': '115052199', 'target_issue': '115052200',
                   'best_strategy': 'ProductionFastPath', 'confidence': 50,
                   'top_k_shadow': frozen, 'model_scores': {},
                   'results': [{'numbers': twenty, 'strategy': 'ProductionFastPath', 'confidence': 50}]}}
    monkeypatch.setattr(service, 'calculate_fast_recommendation', lambda *a, **kw: copy.deepcopy(payload))
    monkeypatch.setattr(service, 'save_prediction_history', store.save_prediction_history)
    result = service.create_for_official_draw('115052199', source='official_collector', trigger='official_draw_saved')
    assert result['status'] == 'created', result
    assert result['persisted'] is True
    def read():
        with connect() as conn:
            return store._row_to_prediction(conn.execute('select ' + store.PREDICTION_SELECT_COLUMNS + ' from prediction_history').fetchone())
    before = read()
    assert before['fast_path_metadata']['top_k_shadow']['top5'] == frozen['top5']
    assert evaluate_frozen_prediction(before)['status'] == 'waiting_draw'
    # A second insert must not replace an already frozen selection.
    changed = copy.deepcopy(before)
    changed['fast_path_metadata']['top_k_shadow']['top5'] = [1, 2, 3, 4, 5]
    duplicate = store.save_prediction_history(changed, caller_context='prediction_service')
    assert duplicate['status'] == 'already_exists'
    assert duplicate['storage'] == backend
    assert read()['fast_path_metadata'] == before['fast_path_metadata']
    # Use real target lookup and real result updater against only the temp DB.
    assert store.update_prediction_history_result({'issue': '115052200', 'numbers': list(range(16, 36)), 'super_number': 16})['updated'] == 1
    after = read()
    assert after['fast_path_metadata'] == before['fast_path_metadata']
    assert after['recommend_numbers'] == twenty
    assert after['model_scores'] == before['model_scores']
    evaluation = evaluate_frozen_prediction(after)
    assert (evaluation['top5_hits'], evaluation['top6_hits']) == (5, 5)
    assert store._enrich_prediction_metadata_from_map(after, {})['top_k_shadow_evaluation'] == evaluation
    assert not sqlite_fallbacks
    service.shutdown_prediction_background_tasks(wait=True)
