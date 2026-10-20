import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api import distribution as api


def test_conditional_download_and_historical_correction(monkeypatch):
    rows = [dict(issue='115059458', numbers=[1, 2], super_number=2,
                 draw_time='2026-10-20T14:10:00+00:00', raw_json={'large': 'payload'})]
    monkeypatch.setattr(api, 'get_official_draws_by_date', lambda date: rows)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        url = '/api/distribution?date=2026-10-20&compact=true'
        first = client.get(url)
        assert first.status_code == 200
        assert 'raw_json' not in first.json()['draws'][0]
        etag = first.headers['etag']
        unchanged = client.get(url, headers={'If-None-Match': etag})
        assert unchanged.status_code == 304
        assert unchanged.content == b''
        rows[0]['numbers'] = [1, 3]
        corrected = client.get(url, headers={'If-None-Match': etag})
        assert corrected.status_code == 200
        assert corrected.headers['etag'] != etag
        assert corrected.json()['draws'][0]['numbers'] == [1, 3]
        full = client.get('/api/distribution?date=2026-10-20')
        assert full.json()['draws'][0]['raw_json'] == {'large': 'payload'}
        assert full.headers['etag'] != corrected.headers['etag']
